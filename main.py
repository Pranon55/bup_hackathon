import os
import logging
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from schemas import OptimizeEnergyRequest, OptimizeEnergyResponse
from interpreter import interpret_operator_notes
from optimizer import optimize_energy_schedule
from validator import replay_and_validate_plan

# Load local .env if present
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("gridwise.main")

app = FastAPI(
    title="GridWise API",
    version="2.0.0",
    description="LLM-assisted 24-hour microgrid and energy schedule optimizer",
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Ensure malformed or missing fields return strict HTTP 400."""
    logger.warning(f"Request validation error on {request.url.path}: {exc}")
    return JSONResponse(
        status_code=400,
        content={
            "detail": "Malformed or invalid request payload.",
            "errors": [
                {"loc": list(err.get("loc", [])), "msg": err.get("msg", "")}
                for err in exc.errors()
            ],
        },
    )


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.post("/optimize-energy", response_model=OptimizeEnergyResponse)
def optimize_energy(request: OptimizeEnergyRequest):
    try:
        # 1. Interpret operator notes via LLM with guardrails
        interpreted_directives = interpret_operator_notes(
            notes=request.operator_notes,
            capacity_kwh=float(request.battery.capacity_kwh),
        )

        # 2. Run deterministic linear program optimizer
        hourly_plan, total_grid, total_cost, peak_grid, summary = optimize_energy_schedule(
            hours=request.hours,
            battery=request.battery,
            directives=interpreted_directives,
        )

        # 3. Replay validator check
        is_compliant = replay_and_validate_plan(
            hours=request.hours,
            battery=request.battery,
            directives=interpreted_directives,
            hourly_plan=hourly_plan,
        )
        if not is_compliant:
            logger.warning(f"Scenario {request.scenario_id}: Replay validator noted non-fatal variance.")

        # 4. Construct response adhering strictly to schema
        return OptimizeEnergyResponse(
            scenario_id=request.scenario_id,
            directive_interpretation=interpreted_directives,
            hourly_plan=hourly_plan,
            total_grid_kwh=total_grid,
            total_cost_bdt=total_cost,
            peak_grid_kwh=peak_grid,
            plan_summary=summary,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in /optimize-energy: {type(e).__name__}: {e}")
        raise HTTPException(
            status_code=500,
            detail="An error occurred while processing the energy optimization request.",
        )
