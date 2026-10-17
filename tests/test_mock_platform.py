from fastapi.testclient import TestClient
from app.mock_platform import app


def test_mock_platform_can_return_rate_limit_with_reset_header():
    with TestClient(app) as client:
        client.post("/control", json={"status_code": 429, "reset_seconds": 7})
        response = client.post("/platform/publish")
        assert response.status_code == 429
        assert response.headers["X-RateLimit-Reset"] == "7"
        client.post("/control", json={"status_code": 200})
