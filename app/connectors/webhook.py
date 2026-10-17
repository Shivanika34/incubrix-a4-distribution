"""Signed generic webhook connector for partner-owned websites."""

import hashlib
import hmac
import json
from typing import Any

from app.connectors.base import Connector
from app.connectors.common import request_with_backoff, validate_rendition
from app.connectors.http import PlatformHttpAdapter
from app.errors import AppError


class WebhookConnector(Connector):
    def __init__(self, manifest: dict[str, Any], adapter: PlatformHttpAdapter, *, simulation_mode: bool, mock_url: str, secrets: dict[str, str]):
        self.manifest, self.adapter = manifest, adapter
        self.simulation_mode, self.mock_url, self.secrets = simulation_mode, mock_url.rstrip("/"), secrets

    def validate(self, rendition, _destination, _connection):
        return validate_rendition(self.manifest, rendition)

    def _secret(self, destination: dict) -> str:
        secret = self.secrets.get(str(destination.get("id", "")))
        if not secret:
            raise AppError("CONNECTION_AUTH_EXPIRED", "No webhook secret is configured for this destination.", True)
        return secret

    def _url(self, destination: dict, connection: dict) -> str:
        if connection.get("local_test") and connection.get("base_url"):
            return f"{connection['base_url'].rstrip('/')}/posts.json"
        return f"{self.mock_url}/platform/webhook" if self.simulation_mode else destination["url"]

    def publish(self, rendition, destination, connection, idempotency_key):
        payload = {"rendition": rendition, "destination": {"id": destination.get("id"), "name": destination.get("name")}}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        signature = hmac.new(self._secret(destination).encode(), encoded, hashlib.sha256).hexdigest()
        response = request_with_backoff(self.adapter, "POST", self._url(destination, connection), headers={"Content-Type": "application/json", "X-IncuBrix-Signature": signature, "Idempotency-Key": idempotency_key or ""}, payload=payload, manifest=self.manifest)
        post_id = str(response.body.get("id", response.body.get("platform_post_id", idempotency_key or "webhook-post")))
        return {"platform_post_id": post_id, "live_url": response.body.get("live_url", destination.get("url")), "status": response.body.get("status", "live")}

    def get_status(self, platform_post_id, connection):
        if connection.get("local_test") and connection.get("base_url"):
            response = request_with_backoff(self.adapter, "GET", f"{connection['base_url'].rstrip('/')}/posts/{platform_post_id}.json", headers={}, payload=None, manifest=self.manifest)
            return {"status": response.body.get("status", "live"), "live_url": response.body.get("url") or response.body.get("live_url")}
        return {"status": "live", "live_url": f"urn:webhook:{platform_post_id}"}

    def delete(self, platform_post_id, connection):
        if connection.get("local_test") and connection.get("base_url"):
            response = request_with_backoff(self.adapter, "DELETE", f"{connection['base_url'].rstrip('/')}/posts/{platform_post_id}.json", headers={}, payload=None, manifest=self.manifest)
            return {"deleted": bool(response.body.get("deleted")), "platform_post_id": platform_post_id}
        return {"deleted": True, "platform_post_id": platform_post_id}
