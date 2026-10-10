from fastapi.testclient import TestClient

import routes.analyze as analyze_route
from main import app
from services.reporting.errors import QuantroError

client = TestClient(app)


def test_run_endpoint_is_registered():
    assert "/analyze/{job_id}/run" in app.openapi()["paths"]


def test_run_endpoint_wraps_success_in_envelope(monkeypatch):
    monkeypatch.setattr(analyze_route, "run_analysis", lambda job_id: {"job_id": job_id})

    response = client.post("/analyze/job_1/run")

    assert response.status_code == 200
    assert response.json() == {"success": True, "result": {"job_id": "job_1"}, "error": None}


def test_run_endpoint_maps_quantro_error(monkeypatch):
    def fail(job_id):
        raise QuantroError(code="SEARCH_FAILED", stage="search_candidates", retryable=True)

    monkeypatch.setattr(analyze_route, "run_analysis", fail)

    response = client.post("/analyze/job_1/run")

    assert response.status_code == 502
    assert response.json()["error"] == {
        "code": "SEARCH_FAILED",
        "stage": "search_candidates",
        "message": "The source search failed.",
        "retryable": True,
    }


def test_run_endpoint_reports_unwired_orchestrator():
    response = client.post("/analyze/job_1/run")

    assert response.status_code == 501
    assert response.json()["success"] is False
