from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import ReadyAdapter


def test_health_is_live(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_checks_dependencies(client):
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_ready_reports_dependency_failure(settings):
    app = create_app(settings=settings, readiness_adapter=ReadyAdapter(should_fail=True))
    with TestClient(app) as client:
        response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
