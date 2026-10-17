"""Dev.to Articles API connector, with a simulation endpoint for local tests."""

from typing import Any

from app.connectors.base import Connector
from app.connectors.common import request_with_backoff, validate_rendition
from app.connectors.http import PlatformHttpAdapter


class DevtoConnector(Connector):
    def __init__(self, manifest: dict[str, Any], adapter: PlatformHttpAdapter, *, simulation_mode: bool, mock_url: str):
        self.manifest, self.adapter = manifest, adapter
        self.simulation_mode, self.mock_url = simulation_mode, mock_url.rstrip("/")

    def validate(self, rendition, _destination, _connection):
        return validate_rendition(self.manifest, rendition)

    def _base_url(self, destination: dict, connection: dict | None = None) -> str:
        connection = connection or {}
        if connection.get("local_test") and connection.get("base_url"):
            return connection["base_url"].rstrip("/")
        if destination.get("local_test") and destination.get("base_url"):
            return destination["base_url"].rstrip("/")
        return f"{self.mock_url}/platform/devto" if self.simulation_mode else destination.get("base_url", "https://dev.to/api")

    @staticmethod
    def _headers(connection: dict) -> dict[str, str]:
        return {"api-key": connection.get("api_key", ""), "Content-Type": "application/json"}

    def publish(self, rendition, destination, connection, _idempotency_key):
        article = {"title": rendition.get("title", ""), "body_markdown": rendition.get("body", ""), "published": True}
        if rendition.get("canonical_url"):
            article["canonical_url"] = rendition["canonical_url"]
        response = request_with_backoff(self.adapter, "POST", f"{self._base_url(destination, connection)}/articles", headers=self._headers(connection), payload={"article": article}, manifest=self.manifest)
        return {"platform_post_id": str(response.body.get("id", "devto-post")), "live_url": response.body.get("url", response.body.get("live_url")), "status": "live"}

    def get_status(self, platform_post_id, connection):
        response = request_with_backoff(self.adapter, "GET", f"{self._base_url({}, connection)}/articles/{platform_post_id}", headers=self._headers(connection), payload=None, manifest=self.manifest)
        return {"status": "live", "live_url": response.body.get("url")}

    def delete(self, platform_post_id, connection):
        response = request_with_backoff(self.adapter, "DELETE", f"{self._base_url({}, connection)}/articles/{platform_post_id}", headers=self._headers(connection), payload=None, manifest=self.manifest)
        return {"deleted": True, "platform_post_id": str(response.body.get("id", platform_post_id))}
