"""Lemmy connector using the community post API."""

from typing import Any

from app.connectors.base import Connector
from app.connectors.common import request_with_backoff, validate_rendition
from app.connectors.http import PlatformHttpAdapter


class LemmyConnector(Connector):
    def __init__(self, manifest: dict[str, Any], adapter: PlatformHttpAdapter, *, simulation_mode: bool, mock_url: str):
        self.manifest, self.adapter = manifest, adapter
        self.simulation_mode, self.mock_url = simulation_mode, mock_url.rstrip("/")

    def validate(self, rendition, _destination, _connection):
        return validate_rendition(self.manifest, rendition)

    def _base_url(self, destination: dict, connection: dict) -> str:
        if connection.get("local_test") and connection.get("base_url"):
            return connection["base_url"].rstrip("/")
        if self.simulation_mode:
            return f"{self.mock_url}/platform/lemmy"
        return (connection.get("base_url") or destination.get("base_url") or destination["url"]).rstrip("/")

    @staticmethod
    def _headers(connection: dict) -> dict[str, str]:
        return {"Content-Type": "application/json"}

    def publish(self, rendition, destination, connection, _idempotency_key):
        payload = {
            "name": rendition.get("title", ""),
            "body": rendition.get("body", ""),
            "community_id": connection.get("community_id") or destination.get("category"),
            "auth": connection.get("auth"),
        }
        response = request_with_backoff(
            self.adapter, "POST", f"{self._base_url(destination, connection)}/api/v3/post",
            headers=self._headers(connection), payload=payload, manifest=self.manifest,
        )
        post = response.body.get("post", response.body)
        post_id = str(post.get("id", response.body.get("id", _idempotency_key or "lemmy-post")))
        live_url = post.get("ap_id") or post.get("url") or response.body.get("live_url") or f"{self._base_url(destination, connection)}/post/{post_id}"
        return {"platform_post_id": post_id, "live_url": live_url, "status": "live"}

    def get_status(self, platform_post_id, connection):
        response = request_with_backoff(
            self.adapter, "GET", f"{self._base_url({}, connection)}/api/v3/post?id={platform_post_id}",
            headers=self._headers(connection), payload=None, manifest=self.manifest,
        )
        post = response.body.get("post", response.body)
        return {"status": "live", "live_url": post.get("ap_id") or post.get("url") or response.body.get("live_url")}

    def delete(self, platform_post_id, connection):
        request_with_backoff(
            self.adapter, "POST", f"{self._base_url({}, connection)}/api/v3/post/delete",
            headers=self._headers(connection), payload={"post_id": platform_post_id, "auth": connection.get("auth")}, manifest=self.manifest,
        )
        return {"deleted": True, "platform_post_id": platform_post_id}
