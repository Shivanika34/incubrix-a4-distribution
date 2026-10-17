from fastapi.testclient import TestClient
from yaml import safe_load

from app.connectors.devto import DevtoConnector
from app.connectors.discourse import DiscourseConnector
from app.connectors.ghost import GhostConnector
from app.connectors.http import PlatformResponse
from app.connectors.lemmy import LemmyConnector
from app.connectors.webhook import WebhookConnector
from app.mock_platform import app as local_platform


class LocalAdapter:
    def __init__(self):
        self.client = TestClient(local_platform)

    def request(self, method, url, *, headers, json=None):
        path = "/" + url.split("/", 3)[-1]
        response = self.client.request(method, path, headers=headers, json=json)
        return PlatformResponse(response.status_code, response.json(), dict(response.headers))


def manifest(key):
    with open(f"connectors/{key}/manifest.yaml", encoding="utf-8") as stream:
        return safe_load(stream)


def test_core_connectors_complete_local_publish_status_delete_lifecycle(monkeypatch):
    adapter = LocalAdapter()
    destination = {"id": "local-destination", "url": "http://local.test/platform", "category": "1"}
    connection = {"base_url": "http://local.test/platform", "local_test": True, "api_key": "local", "admin_token": "local"}
    rendition = {"title": "Local certification", "body": "Disposable local test content."}

    connectors = [
        DiscourseConnector(manifest("discourse"), adapter, simulation_mode=False, mock_url=""),
        LemmyConnector(manifest("lemmy"), adapter, simulation_mode=False, mock_url=""),
        GhostConnector(manifest("ghost"), adapter, simulation_mode=False, mock_url=""),
        DevtoConnector(manifest("devto"), adapter, simulation_mode=False, mock_url=""),
        WebhookConnector(manifest("webhook"), adapter, simulation_mode=False, mock_url="", secrets={"local-destination": "local-secret"}),
    ]
    for connector in connectors:
        published = connector.publish(rendition, destination, connection, f"local-{connector.__class__.__name__}")
        assert published["platform_post_id"]
        status = connector.get_status(published["platform_post_id"], connection)
        assert status["status"] == "live"
        deleted = connector.delete(published["platform_post_id"], connection)
        assert deleted["deleted"] is True
