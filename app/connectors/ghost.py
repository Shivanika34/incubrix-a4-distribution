"""Ghost Admin API connector with server-side Admin API key authentication."""

import base64
import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import quote

from app.connectors.base import Connector
from app.connectors.common import request_with_backoff, validate_rendition
from app.connectors.http import PlatformHttpAdapter
from app.errors import AppError


class GhostConnector(Connector):
    def __init__(self, manifest: dict[str, Any], adapter: PlatformHttpAdapter, *, simulation_mode: bool, mock_url: str):
        self.manifest, self.adapter = manifest, adapter
        self.simulation_mode, self.mock_url = simulation_mode, mock_url.rstrip("/")

    def validate(self, rendition, _destination, _connection):
        return validate_rendition(self.manifest, rendition)

    @staticmethod
    def _base64url(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @classmethod
    def _admin_jwt(cls, api_key: str, now: int | None = None) -> str:
        """Create Ghost's five-minute HS256 integration token without logging it."""
        try:
            key_id, secret_hex = api_key.split(":", 1)
            secret = bytes.fromhex(secret_hex)
        except (ValueError, TypeError):
            raise AppError("CONNECTION_AUTH_EXPIRED", "Ghost Admin API key must be an id and hexadecimal secret.", False)
        if not key_id or not secret:
            raise AppError("CONNECTION_AUTH_EXPIRED", "Ghost Admin API key is invalid.", False)
        issued_at = int(time.time() if now is None else now)
        header = {"alg": "HS256", "typ": "JWT", "kid": key_id}
        payload = {"iat": issued_at, "exp": issued_at + 300, "aud": "/admin/"}
        encoded_header = cls._base64url(json.dumps(header, separators=(",", ":")).encode())
        encoded_payload = cls._base64url(json.dumps(payload, separators=(",", ":")).encode())
        signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
        signature = hmac.new(secret, signing_input, hashlib.sha256).digest()
        return f"{encoded_header}.{encoded_payload}.{cls._base64url(signature)}"

    def _base_url(self, destination: dict, connection: dict) -> str:
        if not self.simulation_mode and connection.get("local_test") and connection.get("base_url"):
            return connection["base_url"].rstrip("/")
        if self.simulation_mode:
            return f"{self.mock_url}/platform/ghost"
        return (connection.get("base_url") or destination.get("base_url") or destination["url"]).rstrip("/")

    def _headers(self, connection: dict) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if connection.get("admin_api_key"):
            headers["Authorization"] = f"Ghost {self._admin_jwt(connection['admin_api_key'])}"
        elif connection.get("admin_token"):
            # Allows pre-generated staff/integration tokens for local compatibility.
            headers["Authorization"] = f"Ghost {connection['admin_token']}"
        if self.manifest.get("api_version"):
            headers["Accept-Version"] = self.manifest["api_version"]
        return headers

    @staticmethod
    def _post(response_body: dict) -> dict:
        posts = response_body.get("posts")
        if not isinstance(posts, list) or not posts or not isinstance(posts[0], dict):
            raise AppError("PLATFORM_UNAVAILABLE", "Ghost returned no post resource.", True)
        post = posts[0]
        if not post.get("id"):
            raise AppError("PLATFORM_UNAVAILABLE", "Ghost response did not include a post id.", True)
        return post

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
        created = self._post(response.body)
        return {
            "platform_post_id": str(created["id"]),
            "live_url": created.get("url") or created.get("canonical_url"),
            "status": created.get("status", "unknown"),
            "updated_at": created.get("updated_at"),
        }

    def get_status(self, platform_post_id, connection):
        response = request_with_backoff(
            self.adapter, "GET", f"{self._base_url({}, connection)}/ghost/api/admin/posts/{platform_post_id}/",
            headers=self._headers(connection), payload=None, manifest=self.manifest,
        )
        post = self._post(response.body)
        if str(post["id"]) != str(platform_post_id):
            raise AppError("CERTIFICATION_FAILED", "Ghost returned a different post than requested.", False)
        return {
            "status": post.get("status", "unknown"),
            "live_url": post.get("url") or post.get("canonical_url"),
            "updated_at": post.get("updated_at"),
            "platform_post_id": str(post["id"]),
        }

    def delete(self, platform_post_id, connection):
        updated_at = connection.get("updated_at")
        if not updated_at:
            raise AppError("CERTIFICATION_FAILED", "Ghost deletion requires the post updated_at value.", False)
        request_with_backoff(
            self.adapter, "DELETE", f"{self._base_url({}, connection)}/ghost/api/admin/posts/{platform_post_id}/?updated_at={quote(str(updated_at), safe='')}",
            headers=self._headers(connection), payload=None, manifest=self.manifest,
        )
        verification = self.adapter.request(
            "GET",
            f"{self._base_url({}, connection)}/ghost/api/admin/posts/{platform_post_id}/",
            headers=self._headers(connection),
            json=None,
        )
        if verification.status_code != 404:
            raise AppError("CERTIFICATION_FAILED", "Ghost delete was not confirmed by a subsequent read.", False, {"platform_status": verification.status_code})
        return {"deleted": True, "deletion_verified": True, "platform_post_id": platform_post_id}
