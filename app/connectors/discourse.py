"""Discourse connector using the stable posts API."""

from typing import Any

from app.connectors.base import Connector
from app.connectors.common import request_with_backoff, validate_rendition
from app.connectors.http import PlatformHttpAdapter


class DiscourseConnector(Connector):
    def __init__(self, manifest: dict[str, Any], adapter: PlatformHttpAdapter, *, simulation_mode: bool, mock_url: str):
        self.manifest, self.adapter = manifest, adapter
        self.simulation_mode, self.mock_url = simulation_mode, mock_url.rstrip("/")

    def validate(self, rendition, _destination, _connection):
        return validate_rendition(self.manifest, rendition)

    def _base_url(self, destination: dict, connection: dict) -> str:
        if connection.get("local_test") and connection.get("base_url"):
            return connection["base_url"].rstrip("/")
        if self.simulation_mode:
            return f"{self.mock_url}/platform/discourse"
        return (connection.get("base_url") or destination.get("base_url") or destination["url"]).rstrip("/")

    @staticmethod
    def _headers(connection: dict) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if connection.get("api_key"):
            headers["Api-Key"] = connection["api_key"]
        if connection.get("api_username"):
            headers["Api-Username"] = connection["api_username"]
        return headers

    def publish(self, rendition, destination, connection, _idempotency_key):
        payload = {
            "title": rendition.get("title", ""),
            "raw": rendition.get("body", ""),
            "category": destination.get("category"),
        }
        if connection.get("topic_id"):
            payload = {"topic_id": connection["topic_id"], "raw": rendition.get("body", "")}
        response = request_with_backoff(
            self.adapter, "POST", f"{self._base_url(destination, connection)}/posts.json",
            headers=self._headers(connection), payload=payload, manifest=self.manifest,
        )
        post_id = str(response.body.get("id", response.body.get("post_id", _idempotency_key or "discourse-post")))
        live_url = response.body.get("url") or response.body.get("live_url") or f"{self._base_url(destination, connection)}/t/{post_id}"
        return {"platform_post_id": post_id, "live_url": live_url, "status": "live"}

    def get_status(self, platform_post_id, connection):
        response = request_with_backoff(
            self.adapter, "GET", f"{self._base_url({}, connection)}/posts/{platform_post_id}.json",
            headers=self._headers(connection), payload=None, manifest=self.manifest,
        )
        return {"status": "live", "live_url": response.body.get("url") or response.body.get("live_url")}

    def delete(self, platform_post_id, connection):
        request_with_backoff(
            self.adapter, "DELETE", f"{self._base_url({}, connection)}/posts/{platform_post_id}.json",
            headers=self._headers(connection), payload=None, manifest=self.manifest,
        )
        return {"deleted": True, "platform_post_id": platform_post_id}
