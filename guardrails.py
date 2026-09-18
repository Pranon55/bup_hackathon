import logging
import math
from typing import List, Dict, Any, Optional
from schemas import DirectiveInterpretation

logger = logging.getLogger("gridwise.guardrails")

ALLOWED_DIRECTIVES = {
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
}


def sanitize_hours(raw_hours: Any) -> List[int]:
    """Clean, deduplicate, filter to 0..23, and sort ascending."""
    if not isinstance(raw_hours, list):
        return []
    cleaned = set()
    for h in raw_hours:
        try:
            h_int = int(h)
            if 0 <= h_int <= 23:
                cleaned.add(h_int)
        except (ValueError, TypeError):
            continue
    return sorted(list(cleaned))


def sanitize_directive(
    raw_item: Dict[str, Any],
    note_idx: int,
    capacity_kwh: float,
) -> DirectiveInterpretation:
    """
    Sanitizes an individual raw directive interpretation dictionary.
    Enforces types, numerical limits, and structural requirements.
    """
    dir_type = str(raw_item.get("directive_type", "no_op")).strip().lower()
    explanation = str(raw_item.get("explanation", "")).strip() or "Standard interpretation."

    if dir_type not in ALLOWED_DIRECTIVES:
        logger.warning(f"Note {note_idx}: Coercing unknown directive_type '{dir_type}' to 'no_op'.")
        dir_type = "no_op"

    raw_adj = raw_item.get("structured_adjustment")

    if dir_type == "no_op" or not isinstance(raw_adj, dict):
        return DirectiveInterpretation(
            note_index=note_idx,
            applies=False,
            directive_type="no_op",
            structured_adjustment=None,
            explanation=explanation,
        )

    # Sanitize hours for all window-based directives
    hours = sanitize_hours(raw_adj.get("hours"))
    if not hours:
        logger.warning(f"Note {note_idx}: Directive '{dir_type}' had empty valid hours, coercing to 'no_op'.")
        return DirectiveInterpretation(
            note_index=note_idx,
            applies=False,
            directive_type="no_op",
            structured_adjustment=None,
            explanation=explanation,
        )

    if dir_type == "solar_reduction":
        try:
            factor = float(raw_adj.get("factor", 1.0))
            if math.isnan(factor) or math.isinf(factor):
                factor = 1.0
            factor = max(0.0, min(1.0, factor))
        except (ValueError, TypeError):
            factor = 1.0

        return DirectiveInterpretation(
            note_index=note_idx,
            applies=True,
            directive_type="solar_reduction",
            structured_adjustment={"hours": hours, "factor": round(factor, 4)},
            explanation=explanation,
        )

    elif dir_type == "minimum_battery_reserve":
        try:
            min_energy = float(raw_adj.get("minimum_energy_kwh", 0.0))
            if math.isnan(min_energy) or math.isinf(min_energy) or min_energy < 0:
                logger.warning(f"Note {note_idx}: Invalid minimum_energy_kwh, coercing to no_op.")
                return DirectiveInterpretation(
                    note_index=note_idx,
                    applies=False,
                    directive_type="no_op",
                    structured_adjustment=None,
                    explanation=explanation,
                )
            if min_energy > capacity_kwh:
                logger.warning(
                    f"Note {note_idx}: minimum_energy_kwh ({min_energy}) > capacity ({capacity_kwh}), clamping to capacity."
                )
                min_energy = capacity_kwh
        except (ValueError, TypeError):
            return DirectiveInterpretation(
                note_index=note_idx,
                applies=False,
                directive_type="no_op",
                structured_adjustment=None,
                explanation=explanation,
            )

        return DirectiveInterpretation(
            note_index=note_idx,
            applies=True,
            directive_type="minimum_battery_reserve",
            structured_adjustment={"hours": hours, "minimum_energy_kwh": round(min_energy, 4)},
            explanation=explanation,
        )

    elif dir_type == "max_grid_window":
        try:
            max_grid = float(raw_adj.get("max_grid_kwh", 0.0))
            if math.isnan(max_grid) or math.isinf(max_grid) or max_grid < 0:
                logger.warning(f"Note {note_idx}: Invalid max_grid_kwh, coercing to no_op.")
                return DirectiveInterpretation(
                    note_index=note_idx,
                    applies=False,
                    directive_type="no_op",
                    structured_adjustment=None,
                    explanation=explanation,
                )
        except (ValueError, TypeError):
            return DirectiveInterpretation(
                note_index=note_idx,
                applies=False,
                directive_type="no_op",
                structured_adjustment=None,
                explanation=explanation,
            )

        return DirectiveInterpretation(
            note_index=note_idx,
            applies=True,
            directive_type="max_grid_window",
            structured_adjustment={"hours": hours, "max_grid_kwh": round(max_grid, 4)},
            explanation=explanation,
        )

    elif dir_type in ("no_charge_window", "no_discharge_window"):
        return DirectiveInterpretation(
            note_index=note_idx,
            applies=True,
            directive_type=dir_type,
            structured_adjustment={"hours": hours},
            explanation=explanation,
        )

    # Fallback to no_op
    return DirectiveInterpretation(
        note_index=note_idx,
        applies=False,
        directive_type="no_op",
        structured_adjustment=None,
        explanation=explanation,
    )


def validate_and_guard_interpretations(
    raw_interpretations: List[Any],
    operator_notes: List[str],
    capacity_kwh: float,
) -> List[DirectiveInterpretation]:
    """
    Validates and standardizes LLM output:
    - Guarantees exactly one entry per note index 0..N-1 in ascending order.
    - Handles missing notes (coerced to no_op) and deduplicates note indices.
    - Strict bounds clamping and schema conformity.
    """
    num_notes = len(operator_notes)
    indexed_entries: Dict[int, Dict[str, Any]] = {}

    for item in raw_interpretations:
        if isinstance(item, dict):
            idx = item.get("note_index")
            try:
                idx_int = int(idx)
                if 0 <= idx_int < num_notes:
                    if idx_int not in indexed_entries:
                        indexed_entries[idx_int] = item
            except (ValueError, TypeError):
                continue

    result: List[DirectiveInterpretation] = []
    for i in range(num_notes):
        if i in indexed_entries:
            sanitized = sanitize_directive(indexed_entries[i], i, capacity_kwh)
        else:
            logger.info(f"Note {i} missing from LLM response. Filling with no_op.")
            sanitized = DirectiveInterpretation(
                note_index=i,
                applies=False,
                directive_type="no_op",
                structured_adjustment=None,
                explanation="No directive provided; default to no-op.",
            )
        result.append(sanitized)

    return result
