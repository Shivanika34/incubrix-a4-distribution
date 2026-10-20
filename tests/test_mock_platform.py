from fastapi.testclient import TestClient
from pathlib import Path
from yaml import safe_load

from app.connectors.devto import DevtoConnector
from app.connectors.discourse import DiscourseConnector
from app.connectors.ghost import GhostConnector
from app.connectors.http import PlatformResponse
from app.connectors.lemmy import LemmyConnector
from app.mock_platform import app


class LocalAdapter:
    def __init__(self):
        self.client = TestClient(app)

    def request(self, method, url, *, headers, json=None):
        path = "/" + url.split("/", 3)[-1]
        response = self.client.request(method, path, headers=headers, json=json)
        return PlatformResponse(response.status_code, response.json(), dict(response.headers))


def _manifest(key):
    return safe_load((Path("connectors") / key / "manifest.yaml").read_text(encoding="utf-8"))


def test_mock_platform_can_return_rate_limit_with_reset_header():
    with TestClient(app) as client:
        client.post("/control", json={"status_code": 429, "reset_seconds": 7})
        response = client.post("/platform/publish")
        assert response.status_code == 429
        assert response.headers["X-RateLimit-Reset"] == "7"
        client.post("/control", json={"status_code": 200})


def test_simulation_prefixed_routes_complete_all_four_connector_lifecycles():
    adapter = LocalAdapter()
    connectors = [
        (GhostConnector(_manifest("ghost"), adapter, simulation_mode=True, mock_url="http://mock.test"), {"url": "https://ghost.example"}, {}),
        (DevtoConnector(_manifest("devto"), adapter, simulation_mode=True, mock_url="http://mock.test"), {"url": "https://dev.to/api"}, {}),
        (DiscourseConnector(_manifest("discourse"), adapter, simulation_mode=True, mock_url="http://mock.test"), {"url": "https://forum.example", "category": "1"}, {}),
        (LemmyConnector(_manifest("lemmy"), adapter, simulation_mode=True, mock_url="http://mock.test"), {"url": "https://lemmy.example", "category": "1"}, {"community_id": 1}),
    ]
    rendition = {"title": "Simulation lifecycle", "body": "Local mock only."}

    for connector, destination, connection in connectors:
        published = connector.publish(rendition, destination, connection, "simulation-lifecycle")
        assert published["platform_post_id"]
        status_connection = {**connection, "updated_at": published.get("updated_at")}
        status = connector.get_status(published["platform_post_id"], status_connection)
        assert status["status"] in {"live", "published"}
        deleted = connector.delete(published["platform_post_id"], status_connection)
        assert deleted["deleted"] is True
