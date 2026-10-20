"""Offline safety checks for the explicitly allowlisted local Lemmy target."""

from yaml import safe_load

from app.connectors.http import PlatformResponse
from app.connectors.lemmy import LemmyConnector
from app.database import Destination
from app.errors import AppError


LOCAL_DESTINATION_ID = "a73d0502-fbc6-4236-8820-ff9ee9cf979a"
LOCAL_BASE_URL = "http://lemmy:8536"
LOCAL_COMMUNITY_ID = 2


class RecordingAdapter:
    def __init__(self, response=None):
        self.calls = []
        self.response = response or PlatformResponse(200, {"post": {"id": 81, "community_id": 2}}, {})

    def request(self, method, url, *, headers, json=None):
        self.calls.append({"method": method, "url": url, "headers": headers, "json": json})
        return self.response


def _manifest():
    with open("connectors/lemmy/manifest.yaml", encoding="utf-8") as stream:
        return safe_load(stream)


def _real_destination():
    return {
        "id": LOCAL_DESTINATION_ID,
        "name": "Real Lemmy Local Test",
        "url": f"{LOCAL_BASE_URL}/",
        "platform_key": "lemmy",
        "surface": "community_post",
        "connector_key": "lemmy",
        "category": str(LOCAL_COMMUNITY_ID),
    }


def _connection(**overrides):
    return {
        "local_test": True,
        "base_url": LOCAL_BASE_URL,
        "community_id": LOCAL_COMMUNITY_ID,
        "auth": "offline-test-token",
        **overrides,
    }


def _insert_destination(app, values):
    with app.state.session_factory.begin() as session:
        session.add(Destination(**values))


def test_normal_lemmy_simulation_routes_to_mock_even_if_connection_has_another_url():
    adapter = RecordingAdapter()
    connector = LemmyConnector(_manifest(), adapter, simulation_mode=True, mock_url="http://mock-platform:8080")
    result = connector.publish(
        {"title": "Offline", "body": "No network"},
        {"id": "ordinary", "url": "https://forum.example.test", "category": "2"},
        {"base_url": "https://forum.example.test", "username": "unused", "password": "unused"},
        "offline-key",
    )
    assert result["platform_post_id"] == "81"
    assert adapter.calls[0]["url"] == "http://mock-platform:8080/platform/lemmy/api/v3/post"
    assert adapter.calls[0]["json"].get("auth") is None


def test_explicit_allowlisted_local_target_routes_to_internal_lemmy():
    adapter = RecordingAdapter()
    connector = LemmyConnector(
        _manifest(),
        adapter,
        simulation_mode=True,
        mock_url="http://mock-platform:8080",
        local_test_destination_id=LOCAL_DESTINATION_ID,
    )
    assert connector._base_url(_real_destination(), _connection()) == LOCAL_BASE_URL


def test_local_status_read_uses_the_allowlisted_instance_without_publishing():
    adapter = RecordingAdapter(PlatformResponse(200, {"post_view": {"post": {"id": 1, "community_id": 2}}}, {}))
    connector = LemmyConnector(
        _manifest(),
        adapter,
        simulation_mode=True,
        mock_url="http://mock-platform:8080",
        local_test_destination_id=LOCAL_DESTINATION_ID,
    )
    connection = _connection(
        destination_id=LOCAL_DESTINATION_ID,
        destination_url=f"{LOCAL_BASE_URL}/",
        connector_key="lemmy",
        surface="community_post",
    )
    status = connector.get_status("1", connection, _real_destination())
    assert status["status"] == "live"
    assert status["platform_post_id"] == "1"
    assert adapter.calls == [{
        "method": "GET",
        "url": f"{LOCAL_BASE_URL}/api/v3/post?id=1",
        "headers": {"Content-Type": "application/json"},
        "json": None,
    }]


def test_dispatch_status_uses_registered_destination_for_local_real_route(app, client):
    connector = app.state.connector_registry.get("lemmy")
    connector.local_test_destination_id = LOCAL_DESTINATION_ID
    adapter = RecordingAdapter(PlatformResponse(200, {"post_view": {"post": {"id": 1, "community_id": 2}}}, {}))
    connector.adapter = adapter
    _insert_destination(app, _real_destination())

    response = client.post(
        "/connector/v1/lemmy/get_status",
        headers={"Idempotency-Key": "lemmy-local-status-dispatch"},
        json={
            "platform_post_id": "1",
            "connection": {
                **_connection(),
                "destination_id": LOCAL_DESTINATION_ID,
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "live"
    assert response.json()["platform_post_id"] == "1"
    assert response.json()["community_id"] == LOCAL_COMMUNITY_ID
    assert adapter.calls == [{
        "method": "GET",
        "url": f"{LOCAL_BASE_URL}/api/v3/post?id=1",
        "headers": {"Content-Type": "application/json"},
        "json": None,
    }]


def test_local_test_flag_cannot_route_an_external_or_other_destination():
    connector = LemmyConnector(
        _manifest(),
        RecordingAdapter(),
        simulation_mode=True,
        mock_url="http://mock-platform:8080",
        local_test_destination_id=LOCAL_DESTINATION_ID,
    )
    for destination, connection in (
        (_real_destination(), _connection(base_url="https://attacker.example")),
        ({**_real_destination(), "id": "another-destination"}, _connection()),
        ({**_real_destination(), "category": "99"}, _connection(community_id=99)),
        ({**_real_destination(), "category": "not-a-community"}, _connection(community_id="not-a-community")),
        ({**_real_destination(), "url": "http://lemmy:8536.evil.example/"}, _connection()),
    ):
        try:
            connector._base_url(destination, connection)
        except AppError as error:
            assert error.code == "REQUEST_VALIDATION_ERROR"
        else:
            raise AssertionError("An unapproved local_test target was accepted")

    misconfigured = LemmyConnector(
        _manifest(),
        RecordingAdapter(),
        simulation_mode=True,
        mock_url="http://mock-platform:8080",
        local_test_destination_id=LOCAL_DESTINATION_ID,
        local_test_base_url="http://attacker.example:8536",
    )
    try:
        misconfigured._base_url(
            _real_destination(),
            _connection(base_url="http://attacker.example:8536"),
        )
    except AppError as error:
        assert error.code == "REQUEST_VALIDATION_ERROR"
    else:
        raise AssertionError("An unsafe configured local Lemmy base URL was accepted")


def test_configuration_check_requires_credentials_for_real_local_target(app, client, monkeypatch):
    monkeypatch.delenv("LEMMY_REAL_USERNAME", raising=False)
    monkeypatch.delenv("LEMMY_REAL_PASSWORD", raising=False)
    connector = app.state.connector_registry.get("lemmy")
    connector.local_test_destination_id = LOCAL_DESTINATION_ID
    destination = _real_destination()
    _insert_destination(app, destination)

    missing = client.post(
        "/api/v1/connectors/lemmy/configuration-check",
        headers={"Idempotency-Key": "lemmy-local-missing-auth"},
        json={"destination_id": LOCAL_DESTINATION_ID, "connection": _connection(auth="")},
    )
    assert missing.status_code == 200
    assert missing.json()["status"] == "missing_configuration"
    assert missing.json()["missing_fields"] == ["auth or username + password"]
    assert missing.json()["ready_for_dispatch"] is False
    assert missing.json()["remote_verified"] is False

    ready = client.post(
        "/api/v1/connectors/lemmy/configuration-check",
        headers={"Idempotency-Key": "lemmy-local-auth-present"},
        json={"destination_id": LOCAL_DESTINATION_ID, "connection": _connection()},
    )
    assert ready.status_code == 200
    assert ready.json()["status"] == "local_real_instance"
    assert ready.json()["execution_target"] == "local_real_instance"
    assert ready.json()["configuration_valid"] is True
    assert ready.json()["ready_for_dispatch"] is True
    assert ready.json()["orchestrator_ready"] is False
    assert ready.json()["remote_verified"] is False
    assert "offline-test-token" not in ready.text


def test_configuration_check_rejects_arbitrary_override_and_preserves_mock_fixture(app, client):
    connector = app.state.connector_registry.get("lemmy")
    connector.local_test_destination_id = LOCAL_DESTINATION_ID
    real_destination = _real_destination()
    mock_destination = {
        "id": "local-lemmy-mock-destination",
        "name": "Existing mock Lemmy destination",
        "url": "http://lemmy-test:8080/platform",
        "platform_key": "lemmy",
        "surface": "community_post",
        "connector_key": "lemmy",
        "category": "1",
    }
    _insert_destination(app, real_destination)
    _insert_destination(app, mock_destination)

    unsafe = client.post(
        "/api/v1/connectors/lemmy/configuration-check",
        headers={"Idempotency-Key": "lemmy-local-unsafe-url"},
        json={
            "destination_id": LOCAL_DESTINATION_ID,
            "connection": _connection(base_url="https://attacker.example"),
        },
    )
    assert unsafe.status_code == 200
    assert unsafe.json()["status"] == "invalid_configuration"
    assert unsafe.json()["ready_for_dispatch"] is False

    mock = client.post(
        "/api/v1/connectors/lemmy/configuration-check",
        headers={"Idempotency-Key": "lemmy-local-mock-fixture"},
        json={
            "destination_id": mock_destination["id"],
            "connection": {
                "local_test": True,
                "base_url": mock_destination["url"],
                "community_id": 1,
            },
        },
    )
    assert mock.status_code == 200
    assert mock.json()["status"] == "local_mock_fixture"
    assert mock.json()["execution_target"] == "local_mock_fixture"
    assert mock.json()["ready_for_dispatch"] is True
    assert mock.json()["remote_verified"] is False


def test_direct_dispatch_rejects_unregistered_external_override_before_network(app, client):
    connector = app.state.connector_registry.get("lemmy")
    connector.local_test_destination_id = LOCAL_DESTINATION_ID
    _insert_destination(app, _real_destination())
    response = client.post(
        "/connector/v1/lemmy/publish",
        headers={"Idempotency-Key": "lemmy-local-unsafe-publish"},
        json={
            "rendition": {"title": "Offline only", "body": "This must not be sent."},
            "destination": _real_destination(),
            "connection": _connection(base_url="https://attacker.example"),
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_VALIDATION_ERROR"
    assert "offline-test-token" not in response.text
