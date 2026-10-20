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
    connection = {
        "base_url": "http://local.test/platform",
        "local_test": True,
        "api_key": "local",
        "admin_token": "local",
        "community_id": 1,
    }
    rendition = {"title": "Local certification", "body": "Disposable local test content."}

    connectors = [
        DiscourseConnector(manifest("discourse"), adapter, simulation_mode=False, mock_url=""),
        LemmyConnector(manifest("lemmy"), adapter, simulation_mode=False, mock_url=""),
        GhostConnector(manifest("ghost"), adapter, simulation_mode=False, mock_url=""),
        DevtoConnector(manifest("devto"), adapter, simulation_mode=False, mock_url=""),
        WebhookConnector(manifest("webhook"), adapter, simulation_mode=False, mock_url="", secrets={"local-destination": "local-secret"}),
    ]
    for connector in connectors:
        connector_connection = connection
        connector_destination = destination
        if isinstance(connector, LemmyConnector):
            # Keep the mock fixture on its exact internal endpoint. The separate
            # real-instance override is allowlisted to a different destination.
            connector_destination = {
                "id": "local-lemmy-fixture",
                "url": "http://lemmy-test:8080/platform",
                "connector_key": "lemmy",
                "surface": "community_post",
                "category": "1",
            }
            connector_connection = {
                "local_test": True,
                "base_url": connector_destination["url"],
                "destination_id": connector_destination["id"],
                "destination_url": connector_destination["url"],
                "connector_key": "lemmy",
                "surface": "community_post",
                "community_id": 1,
            }
        published = connector.publish(rendition, connector_destination, connector_connection, f"local-{connector.__class__.__name__}")
        assert published["platform_post_id"]
        status = connector.get_status(published["platform_post_id"], connector_connection)
        assert status["status"] in {"live", "published"}
        deleted = connector.delete(
            published["platform_post_id"],
            {**connector_connection, "updated_at": status.get("updated_at")},
        )
        assert deleted["deleted"] is True
