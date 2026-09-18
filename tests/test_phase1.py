import json
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_optimize_energy_stub():
    with open("tests/fixtures/sample_cases.json", "r", encoding="utf-8") as f:
        cases = json.load(f)["cases"]

    sample_input = cases[0]["input"]
    response = client.post("/optimize-energy", json=sample_input)
    assert response.status_code == 200
    data = response.json()
    assert data["scenario_id"] == "SAMPLE-01"
    assert len(data["directive_interpretation"]) == len(sample_input["operator_notes"])
    assert len(data["hourly_plan"]) == 24
    assert "total_grid_kwh" in data
    assert "total_cost_bdt" in data
    assert "peak_grid_kwh" in data
    assert "plan_summary" in data
