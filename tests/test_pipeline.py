import json
import pytest
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)


@pytest.fixture
def sample_cases():
    with open("tests/fixtures/sample_cases.json", "r", encoding="utf-8") as f:
        return json.load(f)["cases"]


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_malformed_request_returns_400():
    # Missing battery data entirely
    bad_payload = {
        "scenario_id": "TEST-BAD",
        "operator_notes": ["some note"],
        "hours": [{"hour": 0, "demand_kwh": 10, "solar_kwh": 0, "tariff_bdt_per_kwh": 5}],
    }
    response = client.post("/optimize-energy", json=bad_payload)
    assert response.status_code == 400
    data = response.json()
    assert "detail" in data


def test_all_10_sample_cases_end_to_end(sample_cases):
    """
    Runs each case through the full FastAPI /optimize-energy pipeline:
    LLM/fallback interpreter -> guardrails -> LP optimizer -> replay validator.
    """
    for case in sample_cases:
        case_id = case["id"]
        label = case["label"]
        case_input = case["input"]
        expected = case["expected_output"]

        response = client.post("/optimize-energy", json=case_input)
        assert response.status_code == 200, f"{case_id}: Returned status {response.status_code}: {response.text}"
        data = response.json()

        assert data["scenario_id"] == case_id
        assert len(data["directive_interpretation"]) == len(case_input["operator_notes"])
        assert len(data["hourly_plan"]) == 24

        # Check that total values match recalculated values
        recalculated_grid = round(sum(h["grid_kwh"] for h in data["hourly_plan"]), 4)
        assert abs(data["total_grid_kwh"] - recalculated_grid) < 0.02

        recalculated_peak = max(h["grid_kwh"] for h in data["hourly_plan"])
        assert abs(data["peak_grid_kwh"] - recalculated_peak) < 0.02

        print(f"Pipeline Test {case_id} [{label}]: OK (Cost: {data['total_cost_bdt']}, Ref: {expected['total_cost_bdt']})")
