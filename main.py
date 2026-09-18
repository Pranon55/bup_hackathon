import logging
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from schemas import (
    OptimizeEnergyRequest,
    OptimizeEnergyResponse,
    DirectiveInterpretation,
    HourlyPlanItem,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("gridwise")

app = FastAPI(title="GridWise API", version="1.0.0")


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.post("/optimize-energy", response_model=OptimizeEnergyResponse)
def optimize_energy(request: OptimizeEnergyRequest):
    try:
        interpretations = []
        for i, _ in enumerate(request.operator_notes):
            interpretations.append(
                DirectiveInterpretation(
                    note_index=i,
                    applies=False,
                    directive_type="no_op",
                    structured_adjustment=None,
                    explanation="Phase 1 stub: no-op applied.",
                )
            )

        hourly_plan = []
        total_grid = 0.0
        total_cost = 0.0
        peak_grid = 0.0

        for h in sorted(request.hours, key=lambda x: x.hour):
            grid_kwh = round(float(h.demand_kwh), 4)
            cost = grid_kwh * float(h.tariff_bdt_per_kwh)
            total_grid += grid_kwh
            total_cost += cost
            if grid_kwh > peak_grid:
                peak_grid = grid_kwh

            hourly_plan.append(
                HourlyPlanItem(
                    hour=h.hour,
                    grid_kwh=grid_kwh,
                    solar_used_kwh=0.0,
                    battery_action="idle",
                    battery_kwh=0.0,
                    battery_energy_after_kwh=round(float(request.battery.initial_energy_kwh), 4),
                )
            )

        return OptimizeEnergyResponse(
            scenario_id=request.scenario_id,
            directive_interpretation=interpretations,
            hourly_plan=hourly_plan,
            total_grid_kwh=round(total_grid, 4),
            total_cost_bdt=round(total_cost, 4),
            peak_grid_kwh=round(peak_grid, 4),
            plan_summary="Phase 1 baseline stub: meeting demand from grid with idle battery.",
        )
    except Exception as e:
        logger.error(f"Unexpected error in /optimize-energy: {e}")
        raise HTTPException(status_code=500, detail="Internal server error during energy optimization.")
