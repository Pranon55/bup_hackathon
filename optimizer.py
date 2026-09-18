import logging
from typing import List, Dict, Any, Tuple, Optional
import pulp
from schemas import (
    HourInput,
    BatteryInput,
    DirectiveInterpretation,
    HourlyPlanItem,
)

logger = logging.getLogger("gridwise.optimizer")


def solve_schedule(
    hours: List[HourInput],
    battery: BatteryInput,
    directives: List[DirectiveInterpretation],
    enable_max_grid: bool = True,
    enable_battery_reserve: bool = True,
    enable_all_directives: bool = True,
) -> Optional[Dict[str, Any]]:
    """
    Formulates and solves the 24-hour linear program with PuLP/CBC.
    Returns dictionary with hourly variable values or None if infeasible.
    """
    sorted_hours = sorted(hours, key=lambda x: x.hour)
    if len(sorted_hours) != 24:
        raise ValueError(f"Expected exactly 24 hours, got {len(sorted_hours)}")

    # 1. Compute effective solar and active constraints per hour
    effective_solar: Dict[int, float] = {}
    active_min: Dict[int, float] = {}
    max_charge: Dict[int, float] = {}
    max_discharge: Dict[int, float] = {}
    max_grid: Dict[int, Optional[float]] = {}

    for h_data in sorted_hours:
        h = h_data.hour
        effective_solar[h] = float(h_data.solar_kwh)
        active_min[h] = float(battery.minimum_energy_kwh)
        max_charge[h] = float(battery.max_charge_kwh_per_hour)
        max_discharge[h] = float(battery.max_discharge_kwh_per_hour)
        max_grid[h] = None

    if enable_all_directives:
        for directive in directives:
            if not directive.applies or not directive.structured_adjustment:
                continue

            adj = directive.structured_adjustment
            # adj might be a dict or a Pydantic model
            if hasattr(adj, "model_dump"):
                adj = adj.model_dump()
            elif hasattr(adj, "dict"):
                adj = adj.dict()

            dir_type = directive.directive_type
            d_hours = adj.get("hours", [])

            if dir_type == "solar_reduction":
                factor = float(adj.get("factor", 1.0))
                for h in d_hours:
                    if 0 <= h <= 23:
                        effective_solar[h] = effective_solar[h] * factor

            elif dir_type == "minimum_battery_reserve":
                if enable_battery_reserve:
                    min_reserve = float(adj.get("minimum_energy_kwh", battery.minimum_energy_kwh))
                    for h in d_hours:
                        if 0 <= h <= 23:
                            active_min[h] = max(active_min[h], min_reserve)

            elif dir_type == "no_charge_window":
                for h in d_hours:
                    if 0 <= h <= 23:
                        max_charge[h] = 0.0

            elif dir_type == "no_discharge_window":
                for h in d_hours:
                    if 0 <= h <= 23:
                        max_discharge[h] = 0.0

            elif dir_type == "max_grid_window":
                if enable_max_grid:
                    cap = float(adj.get("max_grid_kwh", 1e9))
                    for h in d_hours:
                        if 0 <= h <= 23:
                            if max_grid[h] is None:
                                max_grid[h] = cap
                            else:
                                max_grid[h] = min(max_grid[h], cap)

    # 2. Build PuLP problem
    prob = pulp.LpProblem("GridWise_Energy_Scheduling", pulp.LpMinimize)

    # Variables
    grid = [pulp.LpVariable(f"grid_{h}", lowBound=0) for h in range(24)]
    solar_used = [pulp.LpVariable(f"solar_used_{h}", lowBound=0, upBound=effective_solar[h]) for h in range(24)]
    charge = [pulp.LpVariable(f"charge_{h}", lowBound=0, upBound=max_charge[h]) for h in range(24)]
    discharge = [pulp.LpVariable(f"discharge_{h}", lowBound=0, upBound=max_discharge[h]) for h in range(24)]
    energy = [pulp.LpVariable(f"energy_{h}", lowBound=active_min[h], upBound=battery.capacity_kwh) for h in range(24)]

    # Constraints
    for h in range(24):
        demand = float(sorted_hours[h].demand_kwh)

        # Energy balance: grid[h] + solar_used[h] + discharge[h] == demand[h] + charge[h]
        prob += grid[h] + solar_used[h] + discharge[h] == demand + charge[h], f"Energy_Balance_{h}"

        # Battery dynamics: energy[h] == energy[h-1] + charge[h] - discharge[h]
        if h == 0:
            prob += energy[h] == float(battery.initial_energy_kwh) + charge[h] - discharge[h], f"Battery_Dynamics_{h}"
        else:
            prob += energy[h] == energy[h - 1] + charge[h] - discharge[h], f"Battery_Dynamics_{h}"

        # Grid cap if active
        if max_grid[h] is not None:
            prob += grid[h] <= max_grid[h], f"Max_Grid_{h}"

    # End of day neutrality
    prob += energy[23] == float(battery.initial_energy_kwh), "End_Of_Day_Neutrality"

    # Objective: minimize total grid cost (with tiny tie-breaker to prefer net idle and solar usage)
    cost_expr = pulp.lpSum(
        [
            grid[h] * float(sorted_hours[h].tariff_bdt_per_kwh)
            - 1e-6 * solar_used[h]
            + 1e-7 * (charge[h] + discharge[h])
            for h in range(24)
        ]
    )
    prob += cost_expr

    # Solve with CBC
    solver = pulp.PULP_CBC_CMD(msg=False)
    status = prob.solve(solver)

    if pulp.LpStatus[status] != "Optimal":
        logger.warning(f"Solver status: {pulp.LpStatus[status]} (not Optimal)")
        return None

    return {
        "grid": [pulp.value(grid[h]) for h in range(24)],
        "solar_used": [pulp.value(solar_used[h]) for h in range(24)],
        "charge": [pulp.value(charge[h]) for h in range(24)],
        "discharge": [pulp.value(discharge[h]) for h in range(24)],
        "energy": [pulp.value(energy[h]) for h in range(24)],
        "effective_solar": effective_solar,
    }


def optimize_energy_schedule(
    hours: List[HourInput],
    battery: BatteryInput,
    directives: List[DirectiveInterpretation],
) -> Tuple[List[HourlyPlanItem], float, float, float, str]:
    """
    Executes optimization with relaxation ladder on infeasibility.
    Returns (hourly_plan, total_grid_kwh, total_cost_bdt, peak_grid_kwh, plan_summary).
    """
    sorted_hours = sorted(hours, key=lambda x: x.hour)

    # Attempt 1: Full directives
    solution = solve_schedule(sorted_hours, battery, directives)

    # Attempt 2: Relax max_grid_window
    if solution is None:
        logger.warning("Attempt 1 infeasible. Relaxing max_grid_window...")
        solution = solve_schedule(sorted_hours, battery, directives, enable_max_grid=False)

    # Attempt 3: Relax minimum_battery_reserve
    if solution is None:
        logger.warning("Attempt 2 infeasible. Relaxing minimum_battery_reserve...")
        solution = solve_schedule(
            sorted_hours, battery, directives, enable_max_grid=False, enable_battery_reserve=False
        )

    # Attempt 4: Relax all directives except physical bounds
    if solution is None:
        logger.warning("Attempt 3 infeasible. Relaxing all directives...")
        solution = solve_schedule(sorted_hours, battery, directives, enable_all_directives=False)

    # If still None, construct baseline pure-grid plan
    if solution is None:
        logger.error("All LP attempts failed. Returning baseline physical plan.")
        hourly_plan = []
        total_grid = 0.0
        total_cost = 0.0
        peak_grid = 0.0
        curr_energy = round(float(battery.initial_energy_kwh), 4)

        for h_data in sorted_hours:
            grid_kwh = round(float(h_data.demand_kwh), 4)
            cost = grid_kwh * float(h_data.tariff_bdt_per_kwh)
            total_grid += grid_kwh
            total_cost += cost
            peak_grid = max(peak_grid, grid_kwh)
            hourly_plan.append(
                HourlyPlanItem(
                    hour=h_data.hour,
                    grid_kwh=grid_kwh,
                    solar_used_kwh=0.0,
                    battery_action="idle",
                    battery_kwh=0.0,
                    battery_energy_after_kwh=curr_energy,
                )
            )
        return (
            hourly_plan,
            round(total_grid, 4),
            round(total_cost, 4),
            round(peak_grid, 4),
            "Fallback baseline plan meeting demand directly from grid.",
        )

    # Post-solve post-processing & exact rounding
    hourly_plan: List[HourlyPlanItem] = []
    current_energy = float(battery.initial_energy_kwh)
    total_grid = 0.0
    total_cost = 0.0
    peak_grid = 0.0

    effective_solar = solution["effective_solar"]

    for h in range(24):
        h_data = sorted_hours[h]
        demand = float(h_data.demand_kwh)
        tariff = float(h_data.tariff_bdt_per_kwh)

        raw_charge = max(0.0, float(solution["charge"][h] or 0.0))
        raw_discharge = max(0.0, float(solution["discharge"][h] or 0.0))
        raw_solar = max(0.0, float(solution["solar_used"][h] or 0.0))

        net = raw_charge - raw_discharge
        if net > 1e-4:
            action = "charge"
            b_kwh = round(net, 4)
            c_val = b_kwh
            d_val = 0.0
        elif net < -1e-4:
            action = "discharge"
            b_kwh = round(-net, 4)
            c_val = 0.0
            d_val = b_kwh
        else:
            action = "idle"
            b_kwh = 0.0
            c_val = 0.0
            d_val = 0.0

        # Cap solar_used to available effective solar and demand + charge - discharge
        max_possible_solar = min(effective_solar[h], max(0.0, demand + c_val - d_val))
        solar_used_kwh = round(min(raw_solar, max_possible_solar), 4)

        # Recompute grid exactly from energy balance
        grid_kwh = round(max(0.0, demand + c_val - d_val - solar_used_kwh), 4)

        # Update cumulative battery energy
        current_energy = current_energy + c_val - d_val
        # Clamp tiny precision errors against capacity/0
        clamped_energy = max(0.0, min(float(battery.capacity_kwh), current_energy))
        battery_energy_after_kwh = round(clamped_energy, 4)

        cost = grid_kwh * tariff
        total_grid += grid_kwh
        total_cost += cost
        if grid_kwh > peak_grid:
            peak_grid = grid_kwh

        hourly_plan.append(
            HourlyPlanItem(
                hour=h,
                grid_kwh=grid_kwh,
                solar_used_kwh=solar_used_kwh,
                battery_action=action,
                battery_kwh=b_kwh,
                battery_energy_after_kwh=battery_energy_after_kwh,
            )
        )

    # Generate descriptive plan summary
    applied_types = [d.directive_type for d in directives if d.applies and d.directive_type != "no_op"]
    if applied_types:
        summary = (
            f"Optimized schedule accounting for {', '.join(set(applied_types))}, "
            f"shifting battery storage to minimize high-tariff grid consumption while preserving neutrality."
        )
    else:
        summary = (
            "Optimized 24-hour schedule leveraging solar generation and battery arbitrage to minimize grid cost."
        )

    return (
        hourly_plan,
        round(total_grid, 4),
        round(total_cost, 4),
        round(peak_grid, 4),
        summary,
    )
