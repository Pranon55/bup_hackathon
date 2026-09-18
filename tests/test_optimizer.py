import json
import pytest
from schemas import HourInput, BatteryInput, DirectiveInterpretation
from optimizer import optimize_energy_schedule


@pytest.fixture
def sample_cases():
    with open("tests/fixtures/sample_cases.json", "r", encoding="utf-8") as f:
        return json.load(f)["cases"]


def test_all_10_sample_cases_optimizer(sample_cases):
    """
    Validates optimizer.py against all 10 sample cases using their ground-truth
    directive interpretations. Verifies physics, bounds, neutrality, and optimal cost.
    """
    passed = 0
    total = len(sample_cases)

    for case in sample_cases:
        case_id = case["id"]
        label = case["label"]
        c_input = case["input"]
        expected = case["expected_output"]

        hours = [HourInput(**h) for h in c_input["hours"]]
        battery = BatteryInput(**c_input["battery"])
        directives = [
            DirectiveInterpretation(**d) for d in expected["directive_interpretation"]
        ]

        hourly_plan, total_grid, total_cost, peak_grid, summary = optimize_energy_schedule(
            hours, battery, directives
        )

        assert len(hourly_plan) == 24, f"{case_id}: Hourly plan must have 24 hours"

        # 1. Verify End of day neutrality: battery_energy_after_kwh at hour 23 == initial_energy_kwh
        assert abs(hourly_plan[23].battery_energy_after_kwh - battery.initial_energy_kwh) < 1e-3, (
            f"{case_id}: End of day neutrality violated. "
            f"Expected {battery.initial_energy_kwh}, got {hourly_plan[23].battery_energy_after_kwh}"
        )

        # 2. Verify hourly constraints
        curr_energy = float(battery.initial_energy_kwh)
        for h_item, h_input in zip(hourly_plan, hours):
            # Energy balance: grid + solar_used + discharge == demand + charge
            c_val = h_item.battery_kwh if h_item.battery_action == "charge" else 0.0
            d_val = h_item.battery_kwh if h_item.battery_action == "discharge" else 0.0

            balance_lhs = round(h_item.grid_kwh + h_item.solar_used_kwh + d_val, 4)
            balance_rhs = round(h_input.demand_kwh + c_val, 4)
            assert abs(balance_lhs - balance_rhs) < 0.02, (
                f"{case_id} H{h_item.hour}: Energy balance failed: {balance_lhs} != {balance_rhs}"
            )

            # Cumulative energy check
            curr_energy = curr_energy + c_val - d_val
            assert abs(curr_energy - h_item.battery_energy_after_kwh) < 0.02, (
                f"{case_id} H{h_item.hour}: Cumulative battery tracking mismatch"
            )

            # Capacity bounds
            assert h_item.battery_energy_after_kwh <= battery.capacity_kwh + 0.01, (
                f"{case_id} H{h_item.hour}: Exceeded battery capacity"
            )

        # 3. Cost optimality: returned cost <= reference total_cost_bdt + 0.01 tolerance
        ref_cost = expected["total_cost_bdt"]
        assert total_cost <= ref_cost + 0.05, (
            f"{case_id} ({label}): Total cost {total_cost} exceeds reference cost {ref_cost}"
        )

        passed += 1
        print(f"Case {case_id} [{label}]: OK (Cost: {total_cost} <= Ref: {ref_cost})")

    assert passed == total
