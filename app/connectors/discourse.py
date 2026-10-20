"""Discourse connector using the stable posts API."""

from typing import Any

from app.connectors.base import Connector
from app.connectors.common import request_with_backoff, validate_rendition
from app.connectors.http import PlatformHttpAdapter
from app.errors import AppError


class DiscourseConnector(Connector):
    def __init__(self, manifest: dict[str, Any], adapter: PlatformHttpAdapter, *, simulation_mode: bool, mock_url: str):
        self.manifest, self.adapter = manifest, adapter
        self.simulation_mode, self.mock_url = simulation_mode, mock_url.rstrip("/")

    def validate(self, rendition, _destination, _connection):
        return validate_rendition(self.manifest, rendition)

    def _base_url(self, destination: dict, connection: dict) -> str:
        if not self.simulation_mode and connection.get("local_test") and connection.get("base_url"):
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
        if connection.get("host_header"):
            headers["Host"] = connection["host_header"]
        return headers

    def publish(self, rendition, destination, connection, _idempotency_key):
        payload = {
            "title": rendition.get("title", ""),
            "raw": rendition.get("body", ""),
            "category": destination.get("category"),
        }
        if connection.get("topic_id"):
            payload = {"topic_id": connection["topic_id"], "raw": rendition.get("body", "")}
        headers = self._headers(connection)
        if _idempotency_key:
            headers["Idempotency-Key"] = _idempotency_key
        response = request_with_backoff(
            self.adapter, "POST", f"{self._base_url(destination, connection)}/posts.json",
            headers=headers, payload=payload, manifest=self.manifest,
        )
        post = self._post_from_response(response.body)
        post_id = str(post["id"])
        return {
            "platform_post_id": post_id,
            "live_url": self._live_url(post, self._base_url(destination, connection), post_id),
            "status": "live",
            "topic_id": post.get("topic_id"),
        }

    def get_status(self, platform_post_id, connection):
        response = request_with_backoff(
            self.adapter, "GET", f"{self._base_url({}, connection)}/posts/{platform_post_id}.json",
            headers=self._headers(connection), payload=None, manifest=self.manifest,
        )
        post = self._post_from_response(response.body)
        if str(post["id"]) != str(platform_post_id):
            raise AppError("CERTIFICATION_FAILED", "Discourse returned a different post than requested.", False)
        expected_topic_id = connection.get("topic_id")
        if expected_topic_id is not None and post.get("topic_id") is not None and str(post["topic_id"]) != str(expected_topic_id):
            raise AppError("CERTIFICATION_FAILED", "Discourse returned a post from a different topic.", False)
        return {
            "status": "deleted" if post.get("deleted") else "live",
            "live_url": self._live_url(post, self._base_url({}, connection), str(platform_post_id)),
            "platform_post_id": str(post["id"]),
            "topic_id": post.get("topic_id"),
            "deleted": bool(post.get("deleted")),
        }

    def delete(self, platform_post_id, connection):
        request_with_backoff(
            self.adapter, "DELETE", f"{self._base_url({}, connection)}/posts/{platform_post_id}.json",
            headers=self._headers(connection), payload=None, manifest=self.manifest,
        )
        verification = self.adapter.request(
            "GET",
            f"{self._base_url({}, connection)}/posts/{platform_post_id}.json",
            headers=self._headers(connection),
            json=None,
        )
        if verification.status_code == 404:
            return {"deleted": True, "deletion_verified": True, "platform_post_id": str(platform_post_id)}
        if verification.status_code == 200:
            post = self._post_from_response(verification.body)
            if str(post["id"]) == str(platform_post_id) and post.get("deleted") is True:
                return {"deleted": True, "deletion_verified": True, "platform_post_id": str(platform_post_id)}
        raise AppError("CERTIFICATION_FAILED", "Discourse delete was not confirmed by a subsequent read.", False)

    @staticmethod
    def _post_from_response(body: dict) -> dict:
        post = body.get("post", body)
        if not isinstance(post, dict) or not post.get("id"):
            raise AppError("PLATFORM_UNAVAILABLE", "Discourse returned no post resource.", True)
        return post

    @staticmethod
    def _live_url(post: dict, base_url: str, post_id: str) -> str:
        return post.get("url") or post.get("full_url") or post.get("live_url") or f"{base_url}/t/{post_id}"
