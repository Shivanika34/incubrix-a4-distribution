"""A deterministic in-memory connector used to prove the framework without a real platform."""

from typing import Any
from app.connectors.base import Connector


class NoopConnector(Connector):
    def __init__(self, manifest: dict[str, Any]):
        self.manifest = manifest
        self.posts: dict[str, dict[str, Any]] = {}

    def validate(self, rendition, _destination, _connection):
        spec = self.manifest["surfaces"][0]["spec"]
        violations = []
        for field, limit_key in (("title", "title_max_chars"), ("body", "body_max_chars")):
            if rendition.get(field) and len(rendition[field]) > spec[limit_key]:
                violations.append({"field": field, "message": f"must be at most {spec[limit_key]} characters"})
        return {"ok": not violations, "violations": violations}

    def publish(self, rendition, destination, _connection, idempotency_key):
        post_id = idempotency_key or f"noop-{len(self.posts) + 1}"
        self.posts.setdefault(post_id, {"rendition": rendition, "destination": destination})
        return {"platform_post_id": post_id, "live_url": f"{destination.get('url', 'https://example.invalid').rstrip('/')}/posts/{post_id}", "status": "live"}

    def get_status(self, platform_post_id, _connection):
        post = self.posts.get(platform_post_id)
        return {"status": "live" if post else "deleted", "live_url": None if not post else f"https://noop.local/posts/{platform_post_id}"}

    def delete(self, platform_post_id, _connection):
        self.posts.pop(platform_post_id, None)
        return {"deleted": True}
