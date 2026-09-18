# GridWise — Industrial Microgrid Energy Scheduler

GridWise is an LLM-assisted 24-hour microgrid and energy storage optimization service built for the **BUP CSE Fest 2026 Hackathon**. It ingests hourly load forecasts, solar PV forecasts, dynamic tariffs, battery specifications, and unstructured operator natural-language notes to produce a cost-optimal, physically compliant 24-hour battery schedule.

---

## 1. Pipeline Architecture

The GridWise scheduling pipeline operates in five deterministic and decoupled stages:

```
[ POST /optimize-energy ]
           │
           ▼
1. Pydantic Input Validation (schemas.py) -> Rejects malformed payloads with HTTP 400
           │
           ▼
2. LLM Interpretation Layer (interpreter.py) -> Gemini 2.5 Flash with deterministic regex fallback
           │
           ▼
3. Pre-Solver Guardrail Layer (guardrails.py) -> Sanitizes bounds, hours, types, and enforces 1:1 note indexing
           │
           ▼
4. LP Energy Optimizer (optimizer.py) -> Solves exact 24-hour LP with PuLP / Coin-OR CBC
           │
           ▼
5. Post-Solver Replay Validator (validator.py) -> Verifies physical balance, battery limits & neutrality
           │
           ▼
[ Response 200 OK ]
```

### Role of the LLM vs. Deterministic Solver
- **LLM (`gemini-2.5-flash`)**: Used **only** as a natural language semantic parser to translate unstructured English operator notes into structured directive objects (`directive_type`, `hours`, and numeric parameters).
- **Guardrail Validator**: Sanitizes and clamps all LLM outputs before they touch the solver. If the LLM generates invalid hours, unknown types, or out-of-range parameters, they are coerced or neutralized to `no_op`.
- **Deterministic Optimizer (PuLP / CBC)**: Performs mathematical linear programming to minimize total grid import cost ($\sum \text{grid}_h \times \text{tariff}_h$) subject to energy conservation, battery dynamics, reserve constraints, and end-of-day neutrality. The LLM has zero involvement in calculation, scheduling, or cost numbers.

---

## 2. Environment Variables

Set the following environment variables in your deployment environment or `.env` file:

| Variable Name | Required | Default | Description |
|---|---|---|---|
| `GEMINI_API_KEY` | Yes (for LLM) | `""` | Google Gemini API key for `gemini-2.5-flash`. If absent or timed out, the deterministic keyword fallback is engaged. |
| `PORT` | Optional | `8080` | Port for the Uvicorn web server (automatically configured by Cloud Run / Render). |
| `TARGET_URL` | Optional | `https://bup-hackathon.onrender.com` | Target URL used by the automated test harness. |

---

## 3. Clean-Machine Setup & Local Run

### Prerequisites
- Python 3.10+ (tested on Python 3.11 and 3.14)
- Coin-OR CBC solver (bundled automatically via `pulp`, or install `coinor-cbc` on Debian/Ubuntu)

### Installation
```bash
# 1. Clone repository
git clone https://github.com/Pranon55/bup_hackathon.git
cd bup_hackathon

# 2. Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: .\venv\Scripts\Activate.ps1

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set Gemini API Key
export GEMINI_API_KEY="your_api_key_here"  # On Windows: $env:GEMINI_API_KEY="your_api_key_here"

# 5. Run local development server
uvicorn main:app --host 0.0.0.0 --port 8080
```

---

## 4. Docker Instructions

### Build & Run Locally
```bash
# Build the Docker image
docker build -t gridwise:latest .

# Run container with dynamic port and Gemini key
docker run -d -p 8080:8080 \
  -e PORT=8080 \
  -e GEMINI_API_KEY="your_api_key_here" \
  --name gridwise-app gridwise:latest
```

---

## 5. API Usage & `curl` Examples

### Health Check
```bash
curl -X GET https://bup-hackathon.onrender.com/health
```
**Response:**
```json
{"status":"ok"}
```

### Energy Optimization Request
```bash
curl -X POST https://bup-hackathon.onrender.com/optimize-energy \
  -H "Content-Type: application/json" \
  -d '{
    "scenario_id": "SAMPLE-01",
    "operator_notes": [
      "Facilities will wash the rooftop solar panels from noon until 2 PM. During cleaning, usable solar should be treated as roughly 25% of the forecast.",
      "The sports office moved next months registration deadline."
    ],
    "hours": [
      {"hour": 0, "demand_kwh": 90, "solar_kwh": 0, "tariff_bdt_per_kwh": 6},
      {"hour": 1, "demand_kwh": 85, "solar_kwh": 0, "tariff_bdt_per_kwh": 6},
      {"hour": 2, "demand_kwh": 80, "solar_kwh": 0, "tariff_bdt_per_kwh": 5},
      {"hour": 3, "demand_kwh": 80, "solar_kwh": 0, "tariff_bdt_per_kwh": 5},
      {"hour": 4, "demand_kwh": 85, "solar_kwh": 0, "tariff_bdt_per_kwh": 5},
      {"hour": 5, "demand_kwh": 95, "solar_kwh": 0, "tariff_bdt_per_kwh": 6},
      {"hour": 6, "demand_kwh": 110, "solar_kwh": 5, "tariff_bdt_per_kwh": 8},
      {"hour": 7, "demand_kwh": 130, "solar_kwh": 20, "tariff_bdt_per_kwh": 10},
      {"hour": 8, "demand_kwh": 150, "solar_kwh": 50, "tariff_bdt_per_kwh": 12},
      {"hour": 9, "demand_kwh": 165, "solar_kwh": 90, "tariff_bdt_per_kwh": 14},
      {"hour": 10, "demand_kwh": 175, "solar_kwh": 130, "tariff_bdt_per_kwh": 16},
      {"hour": 11, "demand_kwh": 180, "solar_kwh": 160, "tariff_bdt_per_kwh": 16},
      {"hour": 12, "demand_kwh": 185, "solar_kwh": 180, "tariff_bdt_per_kwh": 15},
      {"hour": 13, "demand_kwh": 180, "solar_kwh": 170, "tariff_bdt_per_kwh": 14},
      {"hour": 14, "demand_kwh": 170, "solar_kwh": 140, "tariff_bdt_per_kwh": 13},
      {"hour": 15, "demand_kwh": 165, "solar_kwh": 90, "tariff_bdt_per_kwh": 14},
      {"hour": 16, "demand_kwh": 170, "solar_kwh": 45, "tariff_bdt_per_kwh": 18},
      {"hour": 17, "demand_kwh": 185, "solar_kwh": 10, "tariff_bdt_per_kwh": 22},
      {"hour": 18, "demand_kwh": 205, "solar_kwh": 0, "tariff_bdt_per_kwh": 28},
      {"hour": 19, "demand_kwh": 215, "solar_kwh": 0, "tariff_bdt_per_kwh": 30},
      {"hour": 20, "demand_kwh": 205, "solar_kwh": 0, "tariff_bdt_per_kwh": 26},
      {"hour": 21, "demand_kwh": 175, "solar_kwh": 0, "tariff_bdt_per_kwh": 18},
      {"hour": 22, "demand_kwh": 135, "solar_kwh": 0, "tariff_bdt_per_kwh": 10},
      {"hour": 23, "demand_kwh": 105, "solar_kwh": 0, "tariff_bdt_per_kwh": 7}
    ],
    "battery": {
      "capacity_kwh": 220,
      "initial_energy_kwh": 110,
      "minimum_energy_kwh": 40,
      "max_charge_kwh_per_hour": 50,
      "max_discharge_kwh_per_hour": 50
    }
  }'
```

**Expected Response Shape:**
```json
{
  "scenario_id": "SAMPLE-01",
  "directive_interpretation": [
    {
      "note_index": 0,
      "applies": true,
      "directive_type": "solar_reduction",
      "structured_adjustment": {
        "hours": [12, 13],
        "factor": 0.25
      },
      "explanation": "Solar availability is reduced to 25% during panel cleaning window (12:00-14:00)."
    },
    {
      "note_index": 1,
      "applies": false,
      "directive_type": "no_op",
      "structured_adjustment": null,
      "explanation": "Administrative deadline does not affect energy schedule."
    }
  ],
  "hourly_plan": [
    {
      "hour": 0,
      "grid_kwh": 90.0,
      "solar_used_kwh": 0.0,
      "battery_action": "idle",
      "battery_kwh": 0.0,
      "battery_energy_after_kwh": 110.0
    }
  ],
  "total_grid_kwh": 2692.5,
  "total_cost_bdt": 38365.0,
  "peak_grid_kwh": 175.0,
  "plan_summary": "Optimized schedule accounting for solar_reduction..."
}
```

---

## 6. Automated Test Harness

Run the automated test harness against any deployed URL or local server:
```bash
# Test local instance
python tests/test_harness.py http://localhost:8080

# Test live deployed instance
python tests/test_harness.py https://bup-hackathon.onrender.com
```

---

## 7. Known Limitations & Fallback Behaviors

1. **LP Infeasibility Fallback Ladder**:
   If an operator specifies mutually contradictory or physically impossible constraints (e.g., an extremely low `max_grid_window` cap combined with depleted solar and an empty battery), the system executes a graceful relaxation ladder:
   - Attempt 1: Full directive constraints.
   - Attempt 2: Relax `max_grid_window`.
   - Attempt 3: Relax `minimum_battery_reserve`.
   - Attempt 4: Relax all directives.
   - Final fallback: Pure grid meeting demand directly while maintaining battery neutrality.
   *Energy conservation, physical battery boundaries, and end-of-day neutrality are never violated under any circumstance.*

2. **LLM Timeout & Network Failure Fallback**:
   The Gemini 2.5 Flash API call is governed by an 8.0-second HTTP timeout with one automated retry. If the remote AI service is unreachable, timed out, or unauthenticated, the deterministic keyword and regex rule engine in `interpreter.py` automatically resolves operational directives without failing the request.

---

## 8. Dependencies & Credits

- **[FastAPI](https://fastapi.tiangolo.com/)**: High-performance asynchronous API framework.
- **[PuLP](https://coin-or.github.io/pulp/)** & **[Coin-OR CBC](https://github.com/coin-or/Cbc)**: Deterministic Linear Programming solver.
- **[Pydantic v2](https://docs.pydantic.dev/)**: Strict data parsing and model validation.
- **[Google Gemini API](https://ai.google.dev/)**: `gemini-2.5-flash` for natural language operator directive understanding.
- **[HTTPX](https://www.encode.io/httpx/)**: Next-generation HTTP client for resilient outbound LLM calls.
- **[Uvicorn](https://www.uvicorn.org/)**: Lightning-fast ASGI web server implementation.
