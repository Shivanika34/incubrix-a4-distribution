import hashlib
import hmac
import json
from pathlib import Path
import pytest
import httpx
import yaml

from app.connectors.assisted import AssistedConnector
from app.connectors.devto import DevtoConnector
from app.connectors.discourse import DiscourseConnector
from app.connectors.ghost import GhostConnector
from app.connectors.http import PlatformResponse
from app.connectors.lemmy import LemmyConnector
from app.connectors.webhook import WebhookConnector
from app.errors import AppError
from app.connectors.common import request_with_backoff
from app.connectors_api import server_connection



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


def test_webhook_rejects_private_urls_and_missing_response_identity():
    connector = WebhookConnector(manifest("webhook"), SequenceAdapter(PlatformResponse(201, {}, {})), simulation_mode=False, mock_url="", secrets={"dest-1": "secret"})
    try:
        connector.publish({"title": "Hello", "body": "World"}, {"id": "dest-1", "url": "http://127.0.0.1/hook"}, {}, "safe-1")
    except AppError as error:
        assert error.code == "DESTINATION_URL_INVALID"
    else:
        raise AssertionError("private webhook URLs must be rejected")

    public = WebhookConnector(manifest("webhook"), SequenceAdapter(PlatformResponse(201, {}, {})), simulation_mode=False, mock_url="", secrets={"dest-1": "secret"})
    try:
        public.publish({"title": "Hello", "body": "World"}, {"id": "dest-1", "url": "https://partner.example/hook"}, {}, "safe-2")
    except AppError as error:
        assert error.code == "PLATFORM_UNAVAILABLE"
    else:
        raise AssertionError("missing webhook identity must fail")


def test_webhook_does_not_fake_lifecycle_for_remote_endpoints():
    connector = WebhookConnector(manifest("webhook"), SequenceAdapter(), simulation_mode=False, mock_url="", secrets={"dest-1": "secret"})
    for operation in (connector.get_status, connector.delete):
        try:
            operation("partner-1", {})
        except AppError as error:
            assert error.code == "CERTIFICATION_FAILED"
        else:
            raise AssertionError("remote webhook lifecycle must be explicitly unsupported")


def test_read_timeout_is_unknown_without_retry():
    class TimeoutAdapter:
        def __init__(self):
            self.calls = 0

        def request(self, method, url, *, headers, json=None):
            self.calls += 1
            raise httpx.ReadTimeout("platform response timed out")

    adapter = TimeoutAdapter()
    connector = DevtoConnector(manifest("devto"), adapter, simulation_mode=False, mock_url="")
    try:
        connector.publish({"title": "Article", "body": "Body"}, {"base_url": "https://dev.to/api"}, {"api_key": "offline-key"}, "timeout-1")
    except AppError as error:
        assert error.code == "PUBLISH_OUTCOME_UNKNOWN"
        assert "Check the platform before retrying" in error.message
    else:
        raise AssertionError("read timeout must be represented as an uncertain outcome")
    assert adapter.calls == 1


def test_assisted_connector_returns_a_manual_submission_pack_without_network():
    connector = AssistedConnector(manifest("assisted"))
    pack = connector.build_assist_pack({"title": "Draft", "body": "Body", "files": ["s3://bucket/photo.png"]}, {"submit_url": "https://community.example/submit"})
    assert pack["files"] == ["s3://bucket/photo.png"]
    assert pack["caption"] == "Draft\n\nBody"
    assert pack["deep_link"] == "https://community.example/submit"
    assert len(pack["checklist"]) == 4


def test_devto_publishes_with_canonical_url_and_supports_status_and_delete():
    adapter = SequenceAdapter(
        PlatformResponse(201, {"id": 42, "url": "https://dev.to/example/article", "published": True, "user": {"username": "incubrix"}}, {}),
        PlatformResponse(200, {"id": 42, "url": "https://dev.to/example/article", "published": True, "user": {"username": "incubrix"}}, {}),
        PlatformResponse(200, {"id": 42, "url": "https://dev.to/example/article", "published": False, "user": {"username": "incubrix"}}, {}),
        PlatformResponse(200, {"id": 42, "url": "https://dev.to/example/article", "published": False, "user": {"username": "incubrix"}}, {}),
    )
    connector = DevtoConnector(manifest("devto"), adapter, simulation_mode=False, mock_url="")
    destination = {"base_url": "https://dev.to/api"}
    connection = {"api_key": "test-key-only", "username": "incubrix"}
    published = connector.publish({"title": "Article", "body": "Markdown", "canonical_url": "https://incubrix.example/original"}, destination, connection, None)
    assert adapter.requests[0]["headers"]["api-key"] == "test-key-only"
    assert adapter.requests[0]["json"]["article"]["canonical_url"] == "https://incubrix.example/original"
    assert published == {"platform_post_id": "42", "live_url": "https://dev.to/example/article", "status": "live", "username": "incubrix"}
    assert connector.get_status("42", connection)["status"] == "live"
    deleted = connector.delete("42", connection)
    assert deleted["deleted"] is True
    assert deleted["cleanup"] == "unpublished"
    assert adapter.requests[0]["headers"]["Accept"] == "application/vnd.forem.api-v1+json"


def test_devto_server_configuration_builds_canonical_api_url_and_auth_headers(monkeypatch):
    monkeypatch.setenv("DEVTO_REAL_API_KEY", "offline-test-key")
    monkeypatch.setenv("DEVTO_REAL_USERNAME", "offline-user")
    monkeypatch.setenv("DEVTO_REAL_BASE_URL", "https://dev.to/api/")
    connection = server_connection("devto", {})
    adapter = SequenceAdapter(PlatformResponse(201, {"id": 7, "published": True}, {}))
    connector = DevtoConnector(manifest("devto"), adapter, simulation_mode=False, mock_url="")

    connector.publish({"title": "Test", "body": "Offline"}, {}, connection, "offline-1")

    request = adapter.requests[0]
    assert request["method"] == "POST"
    assert request["url"] == "https://dev.to/api/articles"
    assert request["headers"]["api-key"] == "offline-test-key"
    assert request["headers"]["Accept"] == "application/vnd.forem.api-v1+json"
    assert connection["username"] == "offline-user"


def test_devto_403_body_is_not_reflected_in_safe_error():
    secret = "offline-key-must-not-leak"
    adapter = SequenceAdapter(PlatformResponse(403, {"message": f"Forbidden Bots; {secret}"}, {"server": "test-proxy"}))
    connector = DevtoConnector(manifest("devto"), adapter, simulation_mode=False, mock_url="")

    with pytest.raises(AppError) as raised:
        connector.publish({"title": "Test", "body": "Offline"}, {"base_url": "https://dev.to/api"}, {"api_key": secret}, "offline-403")

    assert raised.value.code == "PLATFORM_CONTENT_REJECTED"
    assert raised.value.message == "Platform denied the request (HTTP 403)."
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value.details)


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


def test_devto_rejects_mismatched_article_identity_and_verifies_unpublish():
    adapter = SequenceAdapter(
        PlatformResponse(201, {"id": 42, "url": "https://dev.to/example/article", "published": True, "user": {"username": "incubrix"}}, {}),
        PlatformResponse(200, {"id": 99, "url": "https://dev.to/example/other", "published": True, "user": {"username": "incubrix"}}, {}),
    )
    connector = DevtoConnector(manifest("devto"), adapter, simulation_mode=False, mock_url="")
    connection = {"api_key": "offline-key", "username": "incubrix", "base_url": "https://dev.to/api"}
    connector.publish({"title": "Article", "body": "Body"}, {"base_url": "https://dev.to/api"}, connection, "devto-identity")
    try:
        connector.get_status("42", connection)
    except AppError as error:
        assert error.code == "CERTIFICATION_FAILED"
    else:
        raise AssertionError("a mismatched DEV.to article must fail certification")


def test_discourse_publishes_checks_status_and_deletes():
    adapter = SequenceAdapter(
        PlatformResponse(201, {"id": 11, "topic_id": 21, "url": "https://forum.example/t/hello/11"}, {}),
        PlatformResponse(200, {"id": 11, "topic_id": 21, "url": "https://forum.example/t/hello/11"}, {}),
        PlatformResponse(204, {}, {}),
        PlatformResponse(404, {"message": "not found"}, {}),
    )
    connector = DiscourseConnector(manifest("discourse"), adapter, simulation_mode=False, mock_url="")
    destination = {"base_url": "https://forum.example"}
    connection = {"api_key": "forum-key", "api_username": "incubrix", "base_url": "https://forum.example"}
    published = connector.publish({"title": "Hello", "body": "World"}, destination, connection, "disc-1")
    assert published["platform_post_id"] == "11"
    assert published["topic_id"] == 21
    assert adapter.requests[0]["headers"]["Api-Key"] == "forum-key"
    assert adapter.requests[0]["headers"]["Idempotency-Key"] == "disc-1"
    assert connector.get_status("11", {**connection, "topic_id": 21})["status"] == "live"
    assert connector.delete("11", connection)["deleted"] is True


def test_discourse_rejects_wrong_post_identity_and_topic():
    adapter = SequenceAdapter(
        PlatformResponse(201, {"id": 11, "topic_id": 21, "url": "https://forum.example/t/hello/11"}, {}),
        PlatformResponse(200, {"id": 12, "topic_id": 22, "url": "https://forum.example/t/other/12"}, {}),
    )
    connector = DiscourseConnector(manifest("discourse"), adapter, simulation_mode=False, mock_url="")
    connection = {"api_key": "offline-key", "api_username": "certifier", "base_url": "https://forum.example"}
    published = connector.publish({"title": "Hello", "body": "World"}, {"category": 7}, connection, None)
    assert published["topic_id"] == 21
    try:
        connector.get_status("11", {**connection, "topic_id": 21})
    except AppError as error:
        assert error.code == "CERTIFICATION_FAILED"
    else:
        raise AssertionError("a mismatched Discourse post must fail certification")


def test_lemmy_publishes_checks_status_and_deletes():
    adapter = SequenceAdapter(
        PlatformResponse(200, {"post": {"id": 12, "ap_id": "https://lemmy.example/post/12"}}, {}),
        PlatformResponse(200, {"post": {"id": 12, "ap_id": "https://lemmy.example/post/12"}}, {}),
        PlatformResponse(200, {}, {}),
        PlatformResponse(404, {"message": "not found"}, {}),
    )
    connector = LemmyConnector(manifest("lemmy"), adapter, simulation_mode=False, mock_url="")
    destination = {"base_url": "https://lemmy.example", "category": "7"}
    connection = {"auth": "jwt-token", "base_url": "https://lemmy.example", "community_id": 7}
    published = connector.publish({"title": "Hello", "body": "World"}, destination, connection, "lemmy-1")
    assert published["platform_post_id"] == "12"
    assert adapter.requests[0]["json"]["community_id"] == 7
    assert connector.get_status("12", connection)["status"] == "live"
    assert connector.delete("12", connection)["deleted"] is True


def test_lemmy_uses_login_and_validates_post_view_identity():
    adapter = SequenceAdapter(
        PlatformResponse(200, {"jwt": "jwt-from-login"}, {}),
        PlatformResponse(200, {"post_view": {"post": {"id": 12, "community_id": 7, "url": "https://lemmy.example/post/12"}}}, {}),
        PlatformResponse(200, {"post_view": {"post": {"id": 12, "community_id": 7, "url": "https://lemmy.example/post/12"}}}, {}),
    )
    connector = LemmyConnector(manifest("lemmy"), adapter, simulation_mode=False, mock_url="")
    connection = {
        "base_url": "https://lemmy.example",
        "username_or_email": "certifier",
        "password": "offline-secret",
        "community_id": 7,
    }
    published = connector.publish({"title": "Hello", "body": "World"}, {"category": 7}, connection, None)
    assert published["platform_post_id"] == "12"
    assert adapter.requests[0]["url"].endswith("/api/v3/user/login")
    assert adapter.requests[1]["json"]["auth"] == "jwt-from-login"
    assert connector.get_status("12", {**connection, "auth": "jwt-from-login"})["community_id"] == 7


def test_lemmy_rejects_wrong_community_and_missing_delete_verification():
    connector = LemmyConnector(
        manifest("lemmy"),
        SequenceAdapter(PlatformResponse(200, {"post_view": {"post": {"id": 12, "community_id": 99}}}, {})),
        simulation_mode=False,
        mock_url="",
    )
    try:
        connector.publish({"title": "Hello", "body": "World"}, {"category": 7}, {"auth": "jwt", "base_url": "https://lemmy.example"}, None)
    except AppError as error:
        assert error.code == "CERTIFICATION_FAILED"
    else:
        raise AssertionError("wrong-community posts must fail certification")


def test_ghost_publishes_checks_status_and_deletes():
    adapter = SequenceAdapter(
        PlatformResponse(201, {"posts": [{"id": "13", "url": "https://blog.example/hello/", "status": "published", "updated_at": "2026-01-01T00:00:00.000Z"}]}, {}),
        PlatformResponse(200, {"posts": [{"id": "13", "url": "https://blog.example/hello/", "status": "published", "updated_at": "2026-01-01T00:00:00.000Z"}]}, {}),
        PlatformResponse(204, {}, {}),
        PlatformResponse(404, {"message": "not found"}, {}),
    )
    connector = GhostConnector(manifest("ghost"), adapter, simulation_mode=False, mock_url="")
    destination = {"base_url": "https://blog.example"}
    connection = {"admin_token": "ghost-token", "base_url": "https://blog.example"}
    published = connector.publish({"title": "Hello", "body": "World"}, destination, connection, "ghost-1")
    assert published["platform_post_id"] == "13"
    assert adapter.requests[0]["headers"]["Authorization"] == "Ghost ghost-token"
    status = connector.get_status("13", connection)
    assert status["status"] == "published"
    assert connector.delete("13", {**connection, "updated_at": status["updated_at"]})["deleted"] is True


def test_ghost_rejects_mismatched_status_identity():
    connector = GhostConnector(
        manifest("ghost"),
        SequenceAdapter(PlatformResponse(200, {"posts": [{"id": "different", "status": "published"}]}, {})),
        simulation_mode=False,
        mock_url="",
    )
    try:
        connector.get_status("expected", {"admin_token": "offline-token", "base_url": "https://blog.example"})
    except AppError as error:
        assert error.code == "CERTIFICATION_FAILED"
    else:
        raise AssertionError("Ghost status must reject a different post identity")


def test_simulation_mode_cannot_be_overridden_by_local_test_urls():
    cases = (
        (
            "ghost",
            GhostConnector(manifest("ghost"), SequenceAdapter(), simulation_mode=True, mock_url="http://mock.test"),
            {"url": "https://ghost.example", "local_test": True},
            {"local_test": True, "base_url": "https://attacker.example"},
        ),
        (
            "devto",
            DevtoConnector(manifest("devto"), SequenceAdapter(), simulation_mode=True, mock_url="http://mock.test"),
            {"url": "https://dev.to/api", "base_url": "https://attacker.example", "local_test": True},
            {"local_test": True, "base_url": "https://attacker.example"},
        ),
        (
            "discourse",
            DiscourseConnector(manifest("discourse"), SequenceAdapter(), simulation_mode=True, mock_url="http://mock.test"),
            {"url": "https://forum.example", "local_test": True},
            {"local_test": True, "base_url": "https://attacker.example"},
        ),
    )

    for connector_key, connector, destination, connection in cases:
        assert connector._base_url(destination, connection) == f"http://mock.test/platform/{connector_key}"


def test_manifest_limits_return_field_level_violations():
    connector = DevtoConnector(manifest("devto"), SequenceAdapter(), simulation_mode=False, mock_url="")
    result = connector.validate({"title": "x" * 129, "body": "y" * 100001}, {}, {})
    assert result["ok"] is False
    assert {item["field"] for item in result["violations"]} == {"title", "body"}


def test_assisted_route_builds_pack_and_cannot_publish(client):
    body = {"rendition": {"title": "Draft", "body": "Body"}, "destination": {"url": "https://community.example/submit"}}
    pack = client.post("/connector/v1/assisted/build_assist_pack", headers={"Idempotency-Key": "assisted-pack-1"}, json=body)
    assert pack.status_code == 200
    assert pack.json()["deep_link"] == "https://community.example/submit"
    rejected = client.post("/connector/v1/assisted/publish", headers={"Idempotency-Key": "assisted-publish-1"}, json=body)
    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "CONNECTOR_METHOD_NOT_FOUND"





def test_publish_timeout_is_not_automatically_retried():
    class TimeoutAdapter:
        def __init__(self):
            self.calls = 0

        def request(self, method, url, *, headers, json=None):
            self.calls += 1
            raise TimeoutError("simulated timeout after possible acceptance")

    adapter = TimeoutAdapter()
    manifest = {
        "error_map": {},
    }

    with pytest.raises(TimeoutError):
        request_with_backoff(
            adapter,
            "POST",
            "https://example.invalid/posts",
            headers={},
            payload={"title": "Test post"},
            manifest=manifest,
            sleep=lambda _: None,
        )

    assert adapter.calls == 1



def test_http_timeout_is_not_retried_by_shared_backoff():
    class TimeoutAdapter:
        def __init__(self):
            self.calls = 0

        def request(self, method, url, *, headers, json=None):
            self.calls += 1
            raise httpx.ReadTimeout("simulated response timeout")

    adapter = TimeoutAdapter()

    with pytest.raises(AppError) as raised:
        request_with_backoff(
            adapter,
            "POST",
            "https://example.invalid/posts",
            headers={},
            payload={"title": "Test post"},
            manifest={"error_map": {}},
            sleep=lambda _: None,
        )

    assert raised.value.code == "PUBLISH_OUTCOME_UNKNOWN"
    assert adapter.calls == 1


@pytest.mark.parametrize("error", [httpx.ReadError("response dropped"), httpx.WriteError("connection dropped"), httpx.RemoteProtocolError("invalid response")])
def test_post_send_transport_errors_have_uncertain_outcome_without_retry(error):
    class FailingAdapter:
        calls = 0

        def request(self, method, url, *, headers, json=None):
            self.calls += 1
            raise error

    adapter = FailingAdapter()
    with pytest.raises(AppError) as raised:
        request_with_backoff(adapter, "POST", "https://example.invalid/posts", headers={}, payload={"title": "x"}, manifest={"error_map": {}})
    assert raised.value.code == "PUBLISH_OUTCOME_UNKNOWN"
    assert "Check the platform before retrying" in raised.value.message
    assert adapter.calls == 1


def test_connection_error_is_definitive_unavailable_without_retry():
    class FailingAdapter:
        calls = 0

        def request(self, method, url, *, headers, json=None):
            self.calls += 1
            raise httpx.ConnectError("connection refused")

    adapter = FailingAdapter()
    with pytest.raises(AppError) as raised:
        request_with_backoff(adapter, "POST", "https://example.invalid/posts", headers={}, payload={"title": "x"}, manifest={"error_map": {}})
    assert raised.value.code == "PLATFORM_UNAVAILABLE"
    assert adapter.calls == 1


@pytest.mark.parametrize(("method", "expected_code"), [("GET", "PLATFORM_UNAVAILABLE"), ("DELETE", "PLATFORM_OUTCOME_UNKNOWN")])
def test_non_publish_transport_errors_do_not_claim_publish_uncertainty(method, expected_code):
    class FailingAdapter:
        calls = 0

        def request(self, method, url, *, headers, json=None):
            self.calls += 1
            raise httpx.ReadTimeout("request timed out")

    adapter = FailingAdapter()
    with pytest.raises(AppError) as raised:
        request_with_backoff(adapter, method, "https://example.invalid/posts/1", headers={}, payload=None, manifest={"error_map": {}})
    assert raised.value.code == expected_code
    assert adapter.calls == 1
