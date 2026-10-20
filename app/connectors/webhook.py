"""Signed generic webhook connector for partner-owned websites."""

import hashlib
import hmac
import ipaddress
import json
from typing import Any
from urllib.parse import urlsplit

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
            return self._validate_url(f"{connection['base_url'].rstrip('/')}/posts.json", local=True)
        if self.simulation_mode:
            return f"{self.mock_url}/platform/webhook"
        return self._validate_url(destination.get("url", ""), local=False)

    @staticmethod
    def _validate_url(url: str, *, local: bool) -> str:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        local_hosts = {"localhost", "127.0.0.1", "::1", "local.test", "mock-platform"}
        if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password or parsed.fragment:
            raise AppError("DESTINATION_URL_INVALID", "Webhook URL must be an HTTP(S) URL without credentials or fragments.", False)
        if local:
            if host not in local_hosts and not host.endswith(".local.test"):
                raise AppError("DESTINATION_URL_INVALID", "Local webhook tests may target only an approved local hostname.", False)
            return url
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if host in local_hosts or host.endswith((".local", ".internal", ".test")) or (address and (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved)):
            raise AppError("DESTINATION_URL_INVALID", "Webhook URL targets a private or local network address.", False)
        return url

    @staticmethod
    def _response_id(body: dict[str, Any]) -> str:
        post_id = body.get("id") or body.get("platform_post_id")
        if not post_id:
            raise AppError("PLATFORM_UNAVAILABLE", "Webhook response did not include a stable post ID.", True)
        return str(post_id)

    def publish(self, rendition, destination, connection, idempotency_key):
        payload = {"rendition": rendition, "destination": {"id": destination.get("id"), "name": destination.get("name")}}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        signature = hmac.new(self._secret(destination).encode(), encoded, hashlib.sha256).hexdigest()
        response = request_with_backoff(self.adapter, "POST", self._url(destination, connection), headers={"Content-Type": "application/json", "X-IncuBrix-Signature": signature, "Idempotency-Key": idempotency_key or ""}, payload=payload, manifest=self.manifest)
        post_id = self._response_id(response.body)
        return {"platform_post_id": post_id, "live_url": response.body.get("live_url", destination.get("url")), "status": response.body.get("status", "live")}

    def get_status(self, platform_post_id, connection):
        if connection.get("local_test") and connection.get("base_url"):
            response = request_with_backoff(self.adapter, "GET", f"{self._validate_url(connection['base_url'].rstrip('/') + '/posts/' + str(platform_post_id) + '.json', local=True)}", headers={}, payload=None, manifest=self.manifest)
            if str(response.body.get("id") or response.body.get("platform_post_id")) != str(platform_post_id):
                raise AppError("CERTIFICATION_FAILED", "Webhook status response did not match the published post.", False)
            return {"status": response.body.get("status", "live"), "live_url": response.body.get("url") or response.body.get("live_url")}
        raise AppError("CERTIFICATION_FAILED", "Generic webhooks do not provide a verifiable status endpoint.", False)

    def delete(self, platform_post_id, connection):
        if connection.get("local_test") and connection.get("base_url"):
            response = request_with_backoff(self.adapter, "DELETE", self._validate_url(connection["base_url"].rstrip("/") + "/posts/" + str(platform_post_id) + ".json", local=True), headers={}, payload=None, manifest=self.manifest)
            if not response.body.get("deleted"):
                return {"deleted": False, "platform_post_id": str(platform_post_id)}
            try:
                request_with_backoff(self.adapter, "GET", self._validate_url(connection["base_url"].rstrip("/") + "/posts/" + str(platform_post_id) + ".json", local=True), headers={}, payload=None, manifest=self.manifest)
            except AppError as error:
                if error.details and error.details.get("platform_status") == 404:
                    return {"deleted": True, "deletion_verified": True, "platform_post_id": str(platform_post_id)}
                raise
            raise AppError("CERTIFICATION_FAILED", "Webhook deletion was not verified by a follow-up read.", False)
        raise AppError("CERTIFICATION_FAILED", "Generic webhooks do not provide a verifiable delete endpoint.", False)
