import os
import re
import json
import logging
from typing import List, Dict, Any, Optional
import httpx
from schemas import DirectiveInterpretation
from guardrails import validate_and_guard_interpretations

logger = logging.getLogger("gridwise.interpreter")

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

SYSTEM_INSTRUCTION = """You are the natural language directive interpretation layer for GridWise, an industrial 24-hour microgrid and battery optimization system.
Your job is to read natural language operator notes and classify each note into exactly one of six directive types with exact structured adjustments, or determine that it is an irrelevant distractor.

The six directive types and their exact structured_adjustment shapes are:
1. "solar_reduction": Usable rooftop solar generation is temporarily curtailed or diminished.
   structured_adjustment: {"hours": [int, ...], "factor": float}
   CRITICAL: "factor" is the FRACTION REMAINING (usable fraction), NOT the reduction:
   - "drops to 25% of forecast" -> factor = 0.25
   - "80% reduction" / "reduced by 80%" -> factor = 0.20
   - "about half of forecast" -> factor = 0.50
   - "leaves 30%" -> factor = 0.30

2. "minimum_battery_reserve": Battery energy storage must not drop below a specified reserve level during the window.
   structured_adjustment: {"hours": [int, ...], "minimum_energy_kwh": float}
   CRITICAL: Percentage reserves must be resolved against the battery capacity: minimum_energy_kwh = (percentage / 100) * battery_capacity_kwh.
   Example: If battery_capacity_kwh = 200 and note says "Keep at least 50% stored in battery", minimum_energy_kwh = 100.0.

3. "no_charge_window": Battery charging is completely prohibited/disabled.
   structured_adjustment: {"hours": [int, ...]}

4. "no_discharge_window": Battery discharging is completely prohibited/disabled.
   structured_adjustment: {"hours": [int, ...]}

5. "max_grid_window": Grid import/intake is strictly capped at a maximum kWh limit per hour.
   structured_adjustment: {"hours": [int, ...], "max_grid_kwh": float}

6. "no_op": The note is an unrelated announcement, distractor, administrative notice, deadline, sports notice, library notice, room booking, or does not constrain today's energy operations.
   applies: false
   structured_adjustment: null

TIME WINDOW RULES (START-INCLUSIVE, END-EXCLUSIVE):
- All hour windows represent integers 0 through 23.
- "from 1 PM to 3 PM" -> [13, 14]
- "from 6 PM until 9 PM" -> [18, 19, 20]
- "noon until 2 PM" -> [12, 13]
- "between 11 AM and 2 PM" -> [11, 12, 13]
- "from 6 PM until 10 PM" -> [18, 19, 20, 21]
- "from 2 AM until 5 AM" -> [2, 3, 4]
- "7 PM until 10 PM" -> [19, 20, 21]

OUTPUT FORMAT:
Return a valid JSON array of objects, one per operator note in note_index order:
[
  {
    "note_index": int,
    "applies": bool,
    "directive_type": "solar_reduction" | "minimum_battery_reserve" | "no_charge_window" | "no_discharge_window" | "max_grid_window" | "no_op",
    "structured_adjustment": object or null,
    "explanation": "Brief explanation of the adjustment"
  }
]
"""

FEW_SHOT_PROMPT = """Example 1:
battery_capacity_kwh: 220
Notes:
0: "Facilities will wash the rooftop solar panels from noon until 2 PM. During cleaning, usable solar should be treated as roughly 25% of the forecast."
1: "The sports office moved next month's registration deadline."
Output:
[
  {
    "note_index": 0,
    "applies": true,
    "directive_type": "solar_reduction",
    "structured_adjustment": {"hours": [12, 13], "factor": 0.25},
    "explanation": "Solar availability is reduced to 25% during panel cleaning window (12:00-14:00)."
  },
  {
    "note_index": 1,
    "applies": false,
    "directive_type": "no_op",
    "structured_adjustment": null,
    "explanation": "Administrative deadline does not affect energy schedule."
  }
]

Example 2:
battery_capacity_kwh: 200
Notes:
0: "The battery charger will be isolated from 2 AM until 5 AM for electrical maintenance."
Output:
[
  {
    "note_index": 0,
    "applies": true,
    "directive_type": "no_charge_window",
    "structured_adjustment": {"hours": [2, 3, 4]},
    "explanation": "Battery charging is disabled from 02:00 to 05:00 during charger maintenance."
  }
]

Example 3:
battery_capacity_kwh: 200
Notes:
0: "Keep at least 50% of the battery capacity stored in the battery from 6 PM until 9 PM for emergency operations."
Output:
[
  {
    "note_index": 0,
    "applies": true,
    "directive_type": "minimum_battery_reserve",
    "structured_adjustment": {"hours": [18, 19, 20], "minimum_energy_kwh": 100.0},
    "explanation": "50% of 200 kWh capacity is 100 kWh reserve for hours 18 through 20."
  }
]

Example 4:
battery_capacity_kwh: 230
Notes:
0: "For protection testing, the battery must not discharge from 6 PM until 8 PM."
Output:
[
  {
    "note_index": 0,
    "applies": true,
    "directive_type": "no_discharge_window",
    "structured_adjustment": {"hours": [18, 19]},
    "explanation": "Discharge is prohibited between 18:00 and 20:00."
  }
]

Example 5:
battery_capacity_kwh: 240
Notes:
0: "From 6 PM until 9 PM, campus grid import must not exceed 155 kWh in any hour because the feeder is operating under a temporary limit."
Output:
[
  {
    "note_index": 0,
    "applies": true,
    "directive_type": "max_grid_window",
    "structured_adjustment": {"hours": [18, 19, 20], "max_grid_kwh": 155.0},
    "explanation": "Grid import capped at 155 kWh per hour from 18:00 to 21:00."
  }
]
"""


def parse_time_token(token: str) -> Optional[int]:
    t = token.strip().lower()
    if t == "noon":
        return 12
    if t == "midnight":
        return 0
    m = re.match(r"^(\d+)(?::00)?\s*(am|pm)?$", t)
    if not m:
        return None
    val = int(m.group(1))
    period = m.group(2)
    if period == "am":
        return 0 if val == 12 else val
    elif period == "pm":
        return 12 if val == 12 else val + 12
    elif val <= 24:
        return val
    return None


def extract_hours_from_text(text: str) -> List[int]:
    """Fallback regex extractor for start-inclusive, end-exclusive hours."""
    pattern = r"(?:from|between)\s+([0-9a-zA-Z\s:]+?)\s+(?:until|to|and)\s+([0-9a-zA-Z\s:]+?)(?:\.|\,|$|\s+because|\s+while|\s+during|\s+for|\s+in)"
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        start_str = match.group(1).strip()
        end_str = match.group(2).strip()
        h_start = parse_time_token(start_str)
        h_end = parse_time_token(end_str)
        if h_start is not None and h_end is not None and 0 <= h_start < h_end <= 24:
            return list(range(h_start, h_end))
    return []


def deterministic_fallback_interpret(
    notes: List[str],
    capacity_kwh: float,
) -> List[Dict[str, Any]]:
    """Deterministic keyword and regex rule-based safety net fallback."""
    interpretations: List[Dict[str, Any]] = []

    for i, note in enumerate(notes):
        n_lower = note.lower()
        hours = extract_hours_from_text(note)

        # Distractor checks
        distractor_words = [
            "deadline", "library", "seminar", "club notice", "registration",
            "cafeteria", "menu", "book-return", "sports office", "student affairs",
        ]
        if any(dw in n_lower for dw in distractor_words) and not any(
            kw in n_lower for kw in ["solar", "battery", "charger", "grid", "feeder"]
        ):
            interpretations.append({
                "note_index": i,
                "applies": False,
                "directive_type": "no_op",
                "structured_adjustment": None,
                "explanation": "Deterministic fallback: non-operational administrative notice.",
            })
            continue

        # 1. Solar reduction
        if any(kw in n_lower for kw in ["solar", "panel", "pv", "rooftop"]) and any(
            kw in n_lower for kw in ["wash", "clean", "cloud", "reduc", "drop", "inverter"]
        ):
            factor = 0.5
            pct_match = re.search(r"(\d+)%", n_lower)
            if pct_match:
                pct = float(pct_match.group(1))
                if "reduction" in n_lower or "reduced" in n_lower:
                    factor = max(0.0, min(1.0, 1.0 - (pct / 100.0)))
                else:
                    factor = max(0.0, min(1.0, pct / 100.0))
            elif "half" in n_lower:
                factor = 0.5

            interpretations.append({
                "note_index": i,
                "applies": bool(hours),
                "directive_type": "solar_reduction" if hours else "no_op",
                "structured_adjustment": {"hours": hours, "factor": factor} if hours else None,
                "explanation": "Deterministic fallback: solar reduction.",
            })
            continue

        # 2. No charge window
        if ("charger" in n_lower or "charging" in n_lower) and any(
            kw in n_lower for kw in ["isolated", "disable", "unavail", "maintenance", "inspect"]
        ):
            interpretations.append({
                "note_index": i,
                "applies": bool(hours),
                "directive_type": "no_charge_window" if hours else "no_op",
                "structured_adjustment": {"hours": hours} if hours else None,
                "explanation": "Deterministic fallback: no charging window.",
            })
            continue

        # 3. No discharge window
        if ("discharge" in n_lower or "discharging" in n_lower) and any(
            kw in n_lower for kw in ["not discharge", "disable", "testing", "relay", "protect", "prohibit"]
        ):
            interpretations.append({
                "note_index": i,
                "applies": bool(hours),
                "directive_type": "no_discharge_window" if hours else "no_op",
                "structured_adjustment": {"hours": hours} if hours else None,
                "explanation": "Deterministic fallback: no discharge window.",
            })
            continue

        # 4. Minimum battery reserve
        if any(kw in n_lower for kw in ["reserve", "stored in the battery", "remain in the battery", "emergency"]) and "battery" in n_lower:
            min_kwh = 0.0
            pct_match = re.search(r"(\d+)%", n_lower)
            kwh_match = re.search(r"(\d+(?:\.\d+)?)\s*kwh", n_lower)
            if pct_match:
                pct = float(pct_match.group(1))
                min_kwh = (pct / 100.0) * capacity_kwh
            elif kwh_match:
                min_kwh = float(kwh_match.group(1))

            interpretations.append({
                "note_index": i,
                "applies": bool(hours),
                "directive_type": "minimum_battery_reserve" if hours else "no_op",
                "structured_adjustment": {"hours": hours, "minimum_energy_kwh": min_kwh} if hours else None,
                "explanation": "Deterministic fallback: minimum battery reserve.",
            })
            continue

        # 5. Max grid window
        if any(kw in n_lower for kw in ["grid", "feeder", "transformer", "substation", "import", "intake"]) and any(
            kw in n_lower for kw in ["limit", "cap", "not exceed", "stay at or below"]
        ):
            cap_kwh = 0.0
            kwh_match = re.search(r"(\d+(?:\.\d+)?)\s*kwh", n_lower)
            if kwh_match:
                cap_kwh = float(kwh_match.group(1))

            interpretations.append({
                "note_index": i,
                "applies": bool(hours),
                "directive_type": "max_grid_window" if hours else "no_op",
                "structured_adjustment": {"hours": hours, "max_grid_kwh": cap_kwh} if hours else None,
                "explanation": "Deterministic fallback: max grid import cap.",
            })
            continue

        # Default no_op
        interpretations.append({
            "note_index": i,
            "applies": False,
            "directive_type": "no_op",
            "structured_adjustment": None,
            "explanation": "Deterministic fallback: no recognized operational directive.",
        })

    return interpretations


def call_gemini_api(
    notes: List[str],
    capacity_kwh: float,
    api_key: str,
    timeout_secs: float = 8.0,
) -> Optional[List[Dict[str, Any]]]:
    """Calls Gemini 2.5 Flash API with JSON response format and timeout."""
    url = f"{GEMINI_API_URL}?key={api_key}"

    user_prompt = f"""battery_capacity_kwh: {capacity_kwh}
Operator notes to interpret:
""" + "\n".join(f"{i}: {note}" for i, note in enumerate(notes))

    payload = {
        "systemInstruction": {
            "parts": [{"text": SYSTEM_INSTRUCTION + "\n\n" + FEW_SHOT_PROMPT}]
        },
        "contents": [
            {
                "role": "user",
                "parts": [{"text": user_prompt}],
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.0,
        },
    }

    try:
        with httpx.Client(timeout=timeout_secs) as client:
            resp = client.post(url, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return parsed
                elif isinstance(parsed, dict) and "directives" in parsed:
                    return parsed["directives"]
            else:
                logger.warning(f"Gemini API returned HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        logger.warning(f"Gemini API call failed: {e}")

    return None


def interpret_operator_notes(
    notes: List[str],
    capacity_kwh: float,
) -> List[DirectiveInterpretation]:
    """
    Interprets operator notes through Gemini 2.5 Flash with retry,
    falls back to deterministic interpreter if unavailable,
    and passes everything through guardrails.
    """
    if not notes:
        return []

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    raw_results = None

    if api_key:
        # Attempt 1 with 8s timeout
        raw_results = call_gemini_api(notes, capacity_kwh, api_key, timeout_secs=8.0)

        # Retry once if failed or invalid JSON
        if raw_results is None:
            logger.info("Retrying Gemini API call once...")
            raw_results = call_gemini_api(notes, capacity_kwh, api_key, timeout_secs=8.0)

    if raw_results is None:
        logger.info("Using deterministic fallback interpreter.")
        raw_results = deterministic_fallback_interpret(notes, capacity_kwh)

    # Always pass through Phase 4 Guardrail validator
    return validate_and_guard_interpretations(raw_results, notes, capacity_kwh)
