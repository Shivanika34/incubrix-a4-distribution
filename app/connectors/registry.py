"""Loads and validates connector manifests at startup."""

from pathlib import Path
from typing import Any
import yaml
from jsonschema import Draft202012Validator

from app.connectors.noop import NoopConnector
from app.connectors.assisted import AssistedConnector
from app.connectors.devto import DevtoConnector
from app.connectors.http import HttpxPlatformHttpAdapter
from app.connectors.webhook import WebhookConnector

MANIFEST_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["key", "version", "tier", "auth", "surfaces", "rate_limits", "capabilities", "error_map", "instance_param"],
    "properties": {
        "key": {"type": "string", "pattern": "^[a-z0-9-]+$"}, "version": {"type": ["string", "number"]},
        "tier": {"enum": ["native", "protocol", "bridge", "feed", "assisted"]},
        "auth": {"enum": ["oauth2", "api_key", "bot_token", "none"]},
        "surfaces": {"type": "array", "minItems": 1, "items": {"type": "object", "required": ["key", "content_types", "spec"], "properties": {"key": {"type": "string"}, "content_types": {"type": "array"}, "spec": {"type": "object"}}, "additionalProperties": False}},
        "rate_limits": {"type": "object"}, "capabilities": {"type": "array"}, "error_map": {"type": "object"}, "instance_param": {"type": "string"},
    },
}


def validate_manifest(manifest: dict[str, Any]) -> None:
    errors = sorted(Draft202012Validator(MANIFEST_SCHEMA).iter_errors(manifest), key=lambda err: list(err.path))
    if errors:
        detail = "; ".join(f"{'.'.join(map(str, err.path)) or 'manifest'}: {err.message}" for err in errors)
        raise ValueError(f"Invalid connector manifest: {detail}")


def map_platform_error(status_code: int, error_map: dict[str, str]) -> str:
    return error_map.get(str(status_code), "PLATFORM_UNAVAILABLE" if status_code >= 500 else "PLATFORM_CONTENT_REJECTED")


class ConnectorRegistry:
    def __init__(self, directory: str, settings=None):
        self.manifests: dict[str, dict[str, Any]] = {}
        self.connectors: dict[str, Any] = {}
        for path in sorted(Path(directory).glob("*/manifest.yaml")):
            manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
            validate_manifest(manifest)
            key = manifest["key"]
            if key in self.manifests:
                raise ValueError(f"Duplicate connector key: {key}")
            self.manifests[key] = manifest
            self.connectors[key] = self._build_connector(manifest, settings)

    @staticmethod
    def _build_connector(manifest: dict[str, Any], settings):
        key = manifest["key"]
        if key == "noop":
            return NoopConnector(manifest)
        if key == "assisted":
            return AssistedConnector(manifest)
        adapter = HttpxPlatformHttpAdapter()
        if key == "webhook":
            return WebhookConnector(manifest, adapter, simulation_mode=settings.simulation_mode, mock_url=settings.mock_platform_url, secrets=settings.webhook_secrets)
        if key == "devto":
            return DevtoConnector(manifest, adapter, simulation_mode=settings.simulation_mode, mock_url=settings.mock_platform_url)
        raise ValueError(f"No implementation exists for connector '{key}'.")

    def get(self, key: str):
        return self.connectors.get(key)
