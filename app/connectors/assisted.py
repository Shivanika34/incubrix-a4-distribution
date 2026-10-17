"""Assisted connector: creates a hand-off package and deliberately never publishes."""

from typing import Any
from app.connectors.base import Connector
from app.connectors.common import validate_rendition


class AssistedConnector(Connector):
    def __init__(self, manifest: dict[str, Any]):
        self.manifest = manifest

    def validate(self, rendition, _destination, _connection):
        return validate_rendition(self.manifest, rendition)

    def publish(self, *_args, **_kwargs):
        raise NotImplementedError("Assisted destinations are submitted by a person.")

    def get_status(self, *_args, **_kwargs):
        raise NotImplementedError("Assisted destinations have no platform status API.")

    def delete(self, *_args, **_kwargs):
        raise NotImplementedError("Assisted destinations have no platform delete API.")

    def build_assist_pack(self, rendition, destination):
        files = rendition.get("files", [])
        caption = "\n\n".join(value for value in [rendition.get("title", ""), rendition.get("body", "")] if value).strip()
        return {
            "files": files,
            "caption": caption,
            "deep_link": destination.get("submit_url") or destination.get("url"),
            "checklist": ["Review media rights", "Review text and links", "Confirm destination rules", "Open submit link and post manually"],
        }
