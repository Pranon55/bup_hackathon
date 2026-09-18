import os
import sys
import json
import httpx
from typing import Dict, Any, List

DEFAULT_TARGET_URL = os.getenv("TARGET_URL", "https://bup-hackathon.onrender.com")


def validate_plan_physics(case_id: str, case_input: Dict[str, Any], plan: List[Dict[str, Any]]) -> List[str]:
    errors = []
    if len(plan) != 24:
        errors.append(f"Hourly plan length is {len(plan)}, expected 24")
        return errors

    battery = case_input["battery"]
    hours_input = {h["hour"]: h for h in case_input["hours"]}
    tracked_energy = float(battery["initial_energy_kwh"])

    for h_idx in range(24):
        p = plan[h_idx]
        h_in = hours_input[h_idx]
        demand = float(h_in["demand_kwh"])
        action = p["battery_action"]
        b_kwh = float(p["battery_kwh"])
        c_val = b_kwh if action == "charge" else 0.0
        d_val = b_kwh if action == "discharge" else 0.0

        # Energy balance
        lhs = round(float(p["grid_kwh"]) + float(p["solar_used_kwh"]) + d_val, 4)
        rhs = round(demand + c_val, 4)
        if abs(lhs - rhs) > 0.02:
            errors.append(f"H{h_idx} energy balance: {lhs} != {rhs}")

        # Rate limits
        if c_val > float(battery["max_charge_kwh_per_hour"]) + 0.01:
            errors.append(f"H{h_idx} charge {c_val} > max_charge")
        if d_val > float(battery["max_discharge_kwh_per_hour"]) + 0.01:
            errors.append(f"H{h_idx} discharge {d_val} > max_discharge")

        # Battery tracking
        tracked_energy = tracked_energy + c_val - d_val
        if abs(tracked_energy - float(p["battery_energy_after_kwh"])) > 0.02:
            errors.append(f"H{h_idx} battery tracking mismatch")

        # Capacity bound
        if float(p["battery_energy_after_kwh"]) > float(battery["capacity_kwh"]) + 0.01:
            errors.append(f"H{h_idx} energy > capacity")

    # End of day neutrality
    final_energy = float(plan[23]["battery_energy_after_kwh"])
    if abs(final_energy - float(battery["initial_energy_kwh"])) > 0.02:
        errors.append(f"Neutrality failed: {final_energy} != {battery['initial_energy_kwh']}")

    return errors


def run_harness(base_url: str):
    print("=" * 80)
    print(f"  GridWise Test Harness — Target: {base_url}")
    print("=" * 80)

    # 1. Health check
    try:
        r_health = httpx.get(f"{base_url}/health", timeout=10.0)
        if r_health.status_code != 200:
            print(f"[FAIL] GET /health returned HTTP {r_health.status_code}")
            return False
        print(f"[PASS] GET /health returned 200: {r_health.json()}")
    except Exception as e:
        print(f"[FAIL] Could not connect to /health: {e}")
        return False

    # 2. Load public sample cases
    fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", "sample_cases.json")
    with open(fixture_path, "r", encoding="utf-8") as f:
        cases = json.load(f)["cases"]

    endpoint = f"{base_url}/optimize-energy"
    results = []

    for case in cases:
        case_id = case["id"]
        label = case["label"]
        c_input = case["input"]
        expected = case["expected_output"]

        try:
            resp = httpx.post(endpoint, json=c_input, timeout=30.0)
            if resp.status_code != 200:
                results.append((case_id, label, False, f"HTTP {resp.status_code}: {resp.text[:100]}", 0, 0))
                continue

            data = resp.json()
            # Verify interpretation count
            exp_interp = expected["directive_interpretation"]
            ret_interp = data.get("directive_interpretation", [])
            if len(ret_interp) != len(exp_interp):
                results.append((case_id, label, False, f"Interp count {len(ret_interp)} != {len(exp_interp)}", 0, 0))
                continue

            # Verify physics
            physics_errs = validate_plan_physics(case_id, c_input, data.get("hourly_plan", []))
            if physics_errs:
                results.append((case_id, label, False, f"Physics: {physics_errs[0]}", 0, 0))
                continue

            # Check cost <= reference cost + 0.05
            ret_cost = float(data.get("total_cost_bdt", 1e9))
            ref_cost = float(expected.get("total_cost_bdt", 0))

            if ret_cost > ref_cost + 0.05:
                results.append((case_id, label, False, f"Cost {ret_cost} > ref {ref_cost}", ret_cost, ref_cost))
            else:
                results.append((case_id, label, True, "Optimal & Valid", ret_cost, ref_cost))

        except Exception as e:
            results.append((case_id, label, False, f"Exception: {e}", 0, 0))

    # Print summary table
    print("\n" + "-" * 85)
    print(f"{'Case ID':<12} | {'Label':<32} | {'Status':<6} | {'Ret Cost':<10} | {'Ref Cost':<10} | {'Notes'}")
    print("-" * 85)
    all_passed = True
    for cid, lbl, ok, msg, c_ret, c_ref in results:
        status_str = "PASS" if ok else "FAIL"
        if not ok:
            all_passed = False
        print(f"{cid:<12} | {lbl:<32} | {status_str:<6} | {c_ret:<10.2f} | {c_ref:<10.2f} | {msg}")
    print("-" * 85)

    if all_passed:
        print(f"\n🎉 ALL {len(cases)} SAMPLE CASES PASSED SUCCESSFULLY!")
    else:
        print(f"\n❌ SOME CASES FAILED.")
    return all_passed


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TARGET_URL
    success = run_harness(target)
    sys.exit(0 if success else 1)
