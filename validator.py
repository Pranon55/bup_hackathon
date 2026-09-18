import logging
from typing import List
from schemas import (
    HourInput,
    BatteryInput,
    DirectiveInterpretation,
    HourlyPlanItem,
)

logger = logging.getLogger("gridwise.validator")


def replay_and_validate_plan(
    hours: List[HourInput],
    battery: BatteryInput,
    directives: List[DirectiveInterpretation],
    hourly_plan: List[HourlyPlanItem],
) -> bool:
    """
    Re-checks the final generated hourly_plan against all physical constraints,
    neutrality, and applied directives. Logs any anomalies without crashing.
    Returns True if 100% compliant, False if any warning was logged.
    """
    is_valid = True
    sorted_hours = sorted(hours, key=lambda x: x.hour)

    if len(hourly_plan) != 24:
        logger.error(f"Replay Error: Expected 24 hours in plan, found {len(hourly_plan)}.")
        return False

    # 1. Map directives per hour
    effective_solar = {h.hour: float(h.solar_kwh) for h in sorted_hours}
    active_min_reserve = {h.hour: float(battery.minimum_energy_kwh) for h in sorted_hours}
    no_charge_hours = set()
    no_discharge_hours = set()
    grid_caps = {}

    for d in directives:
        if not d.applies or not d.structured_adjustment:
            continue
        adj = d.structured_adjustment
        if hasattr(adj, "model_dump"):
            adj = adj.model_dump()
        elif hasattr(adj, "dict"):
            adj = adj.dict()

        dir_hours = adj.get("hours", [])
        if d.directive_type == "solar_reduction":
            factor = float(adj.get("factor", 1.0))
            for h in dir_hours:
                effective_solar[h] = effective_solar[h] * factor
        elif d.directive_type == "minimum_battery_reserve":
            m_res = float(adj.get("minimum_energy_kwh", battery.minimum_energy_kwh))
            for h in dir_hours:
                active_min_reserve[h] = max(active_min_reserve[h], m_res)
        elif d.directive_type == "no_charge_window":
            for h in dir_hours:
                no_charge_hours.add(h)
        elif d.directive_type == "no_discharge_window":
            for h in dir_hours:
                no_discharge_hours.add(h)
        elif d.directive_type == "max_grid_window":
            cap = float(adj.get("max_grid_kwh", 1e9))
            for h in dir_hours:
                grid_caps[h] = min(grid_caps.get(h, 1e9), cap)

    # 2. Hourly check
    tracked_energy = float(battery.initial_energy_kwh)

    for item, h_in in zip(hourly_plan, sorted_hours):
        h = item.hour
        demand = float(h_in.demand_kwh)

        charge_kwh = item.battery_kwh if item.battery_action == "charge" else 0.0
        discharge_kwh = item.battery_kwh if item.battery_action == "discharge" else 0.0

        # Energy balance
        lhs = round(item.grid_kwh + item.solar_used_kwh + discharge_kwh, 4)
        rhs = round(demand + charge_kwh, 4)
        if abs(lhs - rhs) > 0.02:
            logger.error(f"Replay Violation H{h}: Energy balance mismatch: {lhs} != {rhs}")
            is_valid = False

        # Solar usage bound
        if item.solar_used_kwh > round(effective_solar[h] + 0.01, 4):
            logger.error(f"Replay Violation H{h}: Solar used ({item.solar_used_kwh}) > effective solar ({effective_solar[h]})")
            is_valid = False

        # Rate limits
        if charge_kwh > round(float(battery.max_charge_kwh_per_hour) + 0.01, 4):
            logger.error(f"Replay Violation H{h}: Charge {charge_kwh} > max charge limit.")
            is_valid = False
        if discharge_kwh > round(float(battery.max_discharge_kwh_per_hour) + 0.01, 4):
            logger.error(f"Replay Violation H{h}: Discharge {discharge_kwh} > max discharge limit.")
            is_valid = False

        # Directive windows
        if h in no_charge_hours and charge_kwh > 0.01:
            logger.warning(f"Replay Warning H{h}: Charging occurred during no_charge_window.")
            is_valid = False
        if h in no_discharge_hours and discharge_kwh > 0.01:
            logger.warning(f"Replay Warning H{h}: Discharging occurred during no_discharge_window.")
            is_valid = False
        if h in grid_caps and item.grid_kwh > round(grid_caps[h] + 0.01, 4):
            logger.warning(f"Replay Warning H{h}: Grid {item.grid_kwh} exceeded cap {grid_caps[h]}.")
            is_valid = False

        # Cumulative energy & reserve bounds
        tracked_energy = tracked_energy + charge_kwh - discharge_kwh
        if abs(tracked_energy - item.battery_energy_after_kwh) > 0.02:
            logger.error(f"Replay Violation H{h}: Battery tracking error: {tracked_energy} != {item.battery_energy_after_kwh}")
            is_valid = False

        if item.battery_energy_after_kwh < round(active_min_reserve[h] - 0.01, 4):
            logger.warning(f"Replay Warning H{h}: Energy {item.battery_energy_after_kwh} < active min {active_min_reserve[h]}")
            is_valid = False
        if item.battery_energy_after_kwh > round(float(battery.capacity_kwh) + 0.01, 4):
            logger.error(f"Replay Violation H{h}: Energy {item.battery_energy_after_kwh} > capacity {battery.capacity_kwh}")
            is_valid = False

    # 3. End-of-day neutrality check at hour 23
    final_energy = hourly_plan[23].battery_energy_after_kwh
    if abs(final_energy - float(battery.initial_energy_kwh)) > 0.02:
        logger.error(
            f"Replay Violation: End-of-day neutrality failed! Final: {final_energy}, Initial: {battery.initial_energy_kwh}"
        )
        is_valid = False

    return is_valid
