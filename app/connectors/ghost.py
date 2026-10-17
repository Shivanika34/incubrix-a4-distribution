"""Ghost Admin API connector with token authentication supplied by the caller."""

from typing import Any

from app.connectors.base import Connector
from app.connectors.common import request_with_backoff, validate_rendition
from app.connectors.http import PlatformHttpAdapter


class GhostConnector(Connector):
    def __init__(self, manifest: dict[str, Any], adapter: PlatformHttpAdapter, *, simulation_mode: bool, mock_url: str):
        self.manifest, self.adapter = manifest, adapter
        self.simulation_mode, self.mock_url = simulation_mode, mock_url.rstrip("/")

    def validate(self, rendition, _destination, _connection):
        return validate_rendition(self.manifest, rendition)

    def _base_url(self, destination: dict, connection: dict) -> str:
        if connection.get("local_test") and connection.get("base_url"):
            return connection["base_url"].rstrip("/")
        if self.simulation_mode:
            return f"{self.mock_url}/platform/ghost"
        return (connection.get("base_url") or destination.get("base_url") or destination["url"]).rstrip("/")

    @staticmethod
    def _headers(connection: dict) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if connection.get("admin_token"):
            headers["Authorization"] = f"Ghost {connection['admin_token']}"
        return headers

    def publish(self, rendition, destination, connection, _idempotency_key):
        post = {
            "title": rendition.get("title", ""),
            "html": rendition.get("body", ""),
            "status": "published",
        }
        if rendition.get("canonical_url"):
            post["canonical_url"] = rendition["canonical_url"]
        response = request_with_backoff(
            self.adapter, "POST", f"{self._base_url(destination, connection)}/ghost/api/admin/posts/?source=html",
            headers=self._headers(connection), payload={"posts": [post]}, manifest=self.manifest,
        )
        posts = response.body.get("posts", [])
        created = posts[0] if posts else response.body
        post_id = str(created.get("id", response.body.get("id", _idempotency_key or "ghost-post")))
        live_url = created.get("url") or created.get("canonical_url") or response.body.get("live_url") or f"{self._base_url(destination, connection)}/{post_id}/"
        return {"platform_post_id": post_id, "live_url": live_url, "status": "live"}

    def get_status(self, platform_post_id, connection):
        response = request_with_backoff(
            self.adapter, "GET", f"{self._base_url({}, connection)}/ghost/api/admin/posts/{platform_post_id}/",
            headers=self._headers(connection), payload=None, manifest=self.manifest,
        )
        posts = response.body.get("posts", [])
        post = posts[0] if posts else response.body
        return {"status": "live", "live_url": post.get("url") or post.get("canonical_url") or response.body.get("live_url")}

    def delete(self, platform_post_id, connection):
        request_with_backoff(
            self.adapter, "DELETE", f"{self._base_url({}, connection)}/ghost/api/admin/posts/{platform_post_id}/",
            headers=self._headers(connection), payload=None, manifest=self.manifest,
        )
        return {"deleted": True, "platform_post_id": platform_post_id}
