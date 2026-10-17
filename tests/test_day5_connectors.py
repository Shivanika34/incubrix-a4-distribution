import hashlib
import hmac
import json
from pathlib import Path

import yaml

from app.connectors.assisted import AssistedConnector
from app.connectors.devto import DevtoConnector
from app.connectors.discourse import DiscourseConnector
from app.connectors.ghost import GhostConnector
from app.connectors.http import PlatformResponse
from app.connectors.lemmy import LemmyConnector
from app.connectors.webhook import WebhookConnector
from app.errors import AppError


class SequenceAdapter:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, *, headers, json=None):
        self.requests.append({"method": method, "url": url, "headers": headers, "json": json})
        return self.responses.pop(0)


def manifest(key):
    return yaml.safe_load((Path("connectors") / key / "manifest.yaml").read_text())


def test_webhook_signs_each_destination_payload_and_maps_revoked_access():
    adapter = SequenceAdapter(PlatformResponse(201, {"id": "partner-1", "live_url": "https://partner.example/posts/1"}, {}))
    connector = WebhookConnector(manifest("webhook"), adapter, simulation_mode=False, mock_url="", secrets={"dest-1": "destination-secret"})
    result = connector.publish({"title": "Hello", "body": "World"}, {"id": "dest-1", "url": "https://partner.example/hook"}, {}, "retry-safe-1")
    expected_payload = {"rendition": {"title": "Hello", "body": "World"}, "destination": {"id": "dest-1", "name": None}}
    encoded = json.dumps(expected_payload, sort_keys=True, separators=(",", ":")).encode()
    assert adapter.requests[0]["headers"]["X-IncuBrix-Signature"] == hmac.new(b"destination-secret", encoded, hashlib.sha256).hexdigest()
    assert adapter.requests[0]["headers"]["Idempotency-Key"] == "retry-safe-1"
    assert result["platform_post_id"] == "partner-1"

    revoked = WebhookConnector(manifest("webhook"), SequenceAdapter(PlatformResponse(401, {"message": "revoked"}, {})), simulation_mode=False, mock_url="", secrets={"dest-1": "destination-secret"})
    try:
        revoked.publish({}, {"id": "dest-1", "url": "https://partner.example/hook"}, {}, None)
    except AppError as error:
        assert error.code == "CONNECTION_AUTH_EXPIRED"
    else:
        raise AssertionError("a revoked webhook should raise a shared authentication error")


def test_assisted_connector_returns_a_manual_submission_pack_without_network():
    connector = AssistedConnector(manifest("assisted"))
    pack = connector.build_assist_pack({"title": "Draft", "body": "Body", "files": ["s3://bucket/photo.png"]}, {"submit_url": "https://community.example/submit"})
    assert pack["files"] == ["s3://bucket/photo.png"]
    assert pack["caption"] == "Draft\n\nBody"
    assert pack["deep_link"] == "https://community.example/submit"
    assert len(pack["checklist"]) == 4


def test_devto_publishes_with_canonical_url_and_supports_status_and_delete():
    adapter = SequenceAdapter(
        PlatformResponse(201, {"id": 42, "url": "https://dev.to/example/article"}, {}),
        PlatformResponse(200, {"url": "https://dev.to/example/article"}, {}),
        PlatformResponse(204, {}, {}),
    )
    connector = DevtoConnector(manifest("devto"), adapter, simulation_mode=False, mock_url="")
    destination = {"base_url": "https://dev.to/api"}
    connection = {"api_key": "test-key-only"}
    published = connector.publish({"title": "Article", "body": "Markdown", "canonical_url": "https://incubrix.example/original"}, destination, connection, None)
    assert adapter.requests[0]["headers"]["api-key"] == "test-key-only"
    assert adapter.requests[0]["json"]["article"]["canonical_url"] == "https://incubrix.example/original"
    assert published == {"platform_post_id": "42", "live_url": "https://dev.to/example/article", "status": "live"}
    assert connector.get_status("42", connection)["status"] == "live"
    assert connector.delete("42", connection)["deleted"] is True


def test_devto_rate_limit_uses_reset_header_before_one_retry(monkeypatch):
    adapter = SequenceAdapter(
        PlatformResponse(429, {"message": "slow down"}, {"X-RateLimit-Reset": "2"}),
        PlatformResponse(201, {"id": 7, "url": "https://dev.to/example/7"}, {}),
    )
    connector = DevtoConnector(manifest("devto"), adapter, simulation_mode=False, mock_url="")
    waits = []
    monkeypatch.setattr("app.connectors.common.time.sleep", waits.append)
    result = connector.publish({"title": "Article", "body": "Body"}, {"base_url": "https://dev.to/api"}, {"api_key": "key"}, None)
    assert waits == [2.0]
    assert len(adapter.requests) == 2
    assert result["platform_post_id"] == "7"


def test_discourse_publishes_checks_status_and_deletes():
    adapter = SequenceAdapter(
        PlatformResponse(201, {"id": 11, "url": "https://forum.example/t/hello/11"}, {}),
        PlatformResponse(200, {"url": "https://forum.example/t/hello/11"}, {}),
        PlatformResponse(204, {}, {}),
    )
    connector = DiscourseConnector(manifest("discourse"), adapter, simulation_mode=False, mock_url="")
    destination = {"base_url": "https://forum.example"}
    connection = {"api_key": "forum-key", "api_username": "incubrix", "base_url": "https://forum.example"}
    published = connector.publish({"title": "Hello", "body": "World"}, destination, connection, "disc-1")
    assert published["platform_post_id"] == "11"
    assert adapter.requests[0]["headers"]["Api-Key"] == "forum-key"
    assert connector.get_status("11", connection)["status"] == "live"
    assert connector.delete("11", connection)["deleted"] is True


def test_lemmy_publishes_checks_status_and_deletes():
    adapter = SequenceAdapter(
        PlatformResponse(200, {"post": {"id": 12, "ap_id": "https://lemmy.example/post/12"}}, {}),
        PlatformResponse(200, {"post": {"id": 12, "ap_id": "https://lemmy.example/post/12"}}, {}),
        PlatformResponse(200, {}, {}),
    )
    connector = LemmyConnector(manifest("lemmy"), adapter, simulation_mode=False, mock_url="")
    destination = {"base_url": "https://lemmy.example", "category": "7"}
    connection = {"auth": "jwt-token", "base_url": "https://lemmy.example"}
    published = connector.publish({"title": "Hello", "body": "World"}, destination, connection, "lemmy-1")
    assert published["platform_post_id"] == "12"
    assert adapter.requests[0]["json"]["community_id"] == "7"
    assert connector.get_status("12", connection)["status"] == "live"
    assert connector.delete("12", connection)["deleted"] is True


def test_ghost_publishes_checks_status_and_deletes():
    adapter = SequenceAdapter(
        PlatformResponse(201, {"posts": [{"id": "13", "url": "https://blog.example/hello/"}]}, {}),
        PlatformResponse(200, {"posts": [{"id": "13", "url": "https://blog.example/hello/"}]}, {}),
        PlatformResponse(204, {}, {}),
    )
    connector = GhostConnector(manifest("ghost"), adapter, simulation_mode=False, mock_url="")
    destination = {"base_url": "https://blog.example"}
    connection = {"admin_token": "ghost-token", "base_url": "https://blog.example"}
    published = connector.publish({"title": "Hello", "body": "World"}, destination, connection, "ghost-1")
    assert published["platform_post_id"] == "13"
    assert adapter.requests[0]["headers"]["Authorization"] == "Ghost ghost-token"
    assert connector.get_status("13", connection)["status"] == "live"
    assert connector.delete("13", connection)["deleted"] is True


def test_manifest_limits_return_field_level_violations():
    connector = DevtoConnector(manifest("devto"), SequenceAdapter(), simulation_mode=False, mock_url="")
    result = connector.validate({"title": "x" * 129, "body": "y" * 100001}, {}, {})
    assert result["ok"] is False
    assert {item["field"] for item in result["violations"]} == {"title", "body"}


def test_assisted_route_builds_pack_and_cannot_publish(client):
    body = {"rendition": {"title": "Draft", "body": "Body"}, "destination": {"url": "https://community.example/submit"}}
    pack = client.post("/connector/v1/assisted/build_assist_pack", json=body)
    assert pack.status_code == 200
    assert pack.json()["deep_link"] == "https://community.example/submit"
    rejected = client.post("/connector/v1/assisted/publish", json=body)
    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "CONNECTOR_METHOD_NOT_FOUND"
