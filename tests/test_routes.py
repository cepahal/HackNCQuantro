from fastapi.testclient import TestClient

import backend.routes.analyze as analyze_route
from backend.main import app
from backend.routes.common import get_tiger_store
from backend.services.shared.errors import QuantroError
from backend.services.reporting.run_analysis import AnalysisResult

client = TestClient(app)


def setup_function():
    app.dependency_overrides[get_tiger_store] = lambda: None


def teardown_function():
    app.dependency_overrides.clear()


def test_routes_are_registered():
    paths = app.openapi()["paths"]
    assert {"/analyze/{job_id}/run", "/reports/{report_id}", "/ingest/{job_id}"} <= set(paths)


def test_run_endpoint_returns_analysis_result(monkeypatch):
    def fake_run(job_id, url, conversation_id, store):
        return AnalysisResult(
            job_id=job_id, outcome="no_verified_match", report_id=f"report_{job_id}",
            report_url=f"https://app.example/reports/report_{job_id}", delivery_status="skipped", warnings=[],
        )

    monkeypatch.setattr(analyze_route, "run_analysis", fake_run)

    response = client.post("/analyze/job_1/run", json={"url": "https://x.com/a"})

    assert response.status_code == 200
    assert response.json() == {
        "job_id": "job_1", "outcome": "no_verified_match", "report_id": "report_job_1",
        "report_url": "https://app.example/reports/report_job_1", "delivery_status": "skipped",
        "delivery_warning": None, "warnings": [],
    }


def test_run_endpoint_maps_quantro_error(monkeypatch):
    def fail(job_id, url, conversation_id, store):
        raise QuantroError(code="SEARCH_FAILED", stage="search_candidates", retryable=True)

    monkeypatch.setattr(analyze_route, "run_analysis", fail)

    response = client.post("/analyze/job_1/run", json={"url": "https://x.com/a"})

    assert response.status_code == 500
    assert response.json() == {"error": {
        "code": "SEARCH_FAILED", "stage": "search_candidates",
        "message": "The source search failed.", "retryable": True,
    }}


def test_run_endpoint_requires_url():
    assert client.post("/analyze/job_1/run", json={}).status_code == 422
