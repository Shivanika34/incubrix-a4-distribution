"""Offline tests for connector discovery/config checks and the same-origin demo."""

import time
from unittest.mock import MagicMock

import pytest
from app.connectors_api import _server_configured, server_connection


def _destination(client, connector_key, surface, url):
    response = client.post(
        "/api/v1/destinations",
        headers={"Idempotency-Key": f"ui-destination-{connector_key}"},
        json={
            "name": f"{connector_key} UI test",
            "url": url,
            "platform_key": connector_key,
            "surface": surface,
            "connector_key": connector_key,
            "category": "17" if connector_key in {"discourse", "lemmy"} else None,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _wait_for_job(client, job_id):
    for _ in range(300):
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "partial", "failed"}:
            return job
        time.sleep(0.01)
    raise AssertionError("Publishing orchestration did not finish within the test deadline")


def test_root_serves_same_origin_publishing_demo(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "IncuBrix publishing demo" in response.text
    # Guard the read-only community browser's user-visible contract. This is a
    # static route test; interactive filtering still needs browser verification.
    assert 'id="discourseCategory"' in response.text
    assert 'option value="">All categories</option>' in response.text
    assert "topic.category_id" in response.text
    assert "addEventListener('change',renderDiscourseTopics)" in response.text
    assert "No public topics match this category." in response.text
    assert "Open original discussion" in response.text
    assert "noopener noreferrer" in response.text
    assert "Public Discourse community unavailable" in response.text
    assert "data-connection-field" not in response.text
    assert "Admin API key" not in response.text
    assert "Preview selected destinations" in response.text
    assert "outcome_unknown" in response.text
    assert "/api/v1/connectors" in response.text
    assert "/api/v1/publishing-orchestrations/preview" in response.text


def test_connector_catalogue_reports_status_without_credentials(client):
    response = client.get("/api/v1/connectors")
    assert response.status_code == 200
    data = response.json()
    assert data["simulation_mode"] is True
    entries = {item["key"]: item for item in data["connectors"]}
    assert set(entries) == {"ghost", "devto", "discourse", "lemmy"}
    assert all(item["registered"] and item["enabled"] for item in entries.values())
    assert all("configuration_fields" not in item for item in entries.values())
    assert all(isinstance(item["server_configured"], bool) for item in entries.values())
    assert "Admin API key" not in response.text


@pytest.mark.parametrize(
    ("connector", "surface", "url", "connection", "secret"),
    [
        ("ghost", "post", "https://ghost.example", {"admin_api_key": "1234567890:deadbeef"}, "1234567890:deadbeef"),
        ("devto", "article", "https://dev.to/api", {"api_key": "devto-secret"}, "devto-secret"),
        ("discourse", "topic", "https://discourse.example", {"api_key": "discourse-secret", "api_username": "ui-test"}, "discourse-secret"),
        ("lemmy", "community_post", "https://lemmy.example", {"username": "ui-test", "password": "lemmy-secret"}, "lemmy-secret"),
    ],
)
def test_configuration_check_is_shape_only_and_never_echoes_secret(client, connector, surface, url, connection, secret):
    destination = _destination(client, connector, surface, url)
    response = client.post(
        f"/api/v1/connectors/{connector}/configuration-check",
        headers={"Idempotency-Key": f"ui-check-{connector}"},
        json={"destination_id": destination["id"], "connection": connection},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["ready_for_dispatch"] is True
    assert result["remote_verified"] is False
    assert secret not in response.text


def test_configuration_check_requires_the_idempotency_header(client):
    destination = _destination(client, "devto", "article", "https://dev.to/api")
    response = client.post(
        "/api/v1/connectors/devto/configuration-check",
        json={"destination_id": destination["id"], "connection": {"api_key": "not-echoed"}},
    )
    assert response.status_code == 422
    assert "not-echoed" not in response.text


@pytest.mark.parametrize(
    ("connector", "environment", "expected"),
    [
        ("ghost", {"GHOST_REAL_ADMIN_API_KEY": "ghost-admin-key"}, {"admin_api_key": "ghost-admin-key"}),
        ("devto", {"DEVTO_REAL_API_KEY": "devto-key", "DEVTO_REAL_USERNAME": "writer"}, {"api_key": "devto-key", "username": "writer"}),
        ("discourse", {"DISCOURSE_REAL_API_KEY": "forum-key", "DISCOURSE_REAL_API_USERNAME": "publisher", "DISCOURSE_HOST_HEADER": "localhost:3000"}, {"api_key": "forum-key", "api_username": "publisher", "host_header": "localhost:3000"}),
        ("lemmy", {"LEMMY_REAL_USERNAME": "user@example.test", "LEMMY_REAL_PASSWORD": "local-password"}, {"username": "user@example.test", "password": "local-password"}),
    ],
)
def test_established_server_credentials_are_available_without_echo(connector, environment, expected, monkeypatch):
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    resolved = server_connection(connector, {"url": "https://destination.example"})
    assert {name: resolved[name] for name in expected} == expected
    assert _server_configured(connector)


def test_power_shell_env_file_loads_without_overriding_process_environment(tmp_path, monkeypatch):
    from app.config import Settings

    env_file = tmp_path / ".env"
    env_file.write_text(
        "DISCOURSE_BASE_URL=http://discourse:3000\n"
        "DISCOURSE_PUBLIC_URL=http://127.0.0.1:3000\n"
        "GHOST_REAL_ADMIN_API_KEY=dotenv-ghost-key\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("INCUBRIX_ENV_FILE", str(env_file))
    monkeypatch.delenv("DISCOURSE_BASE_URL", raising=False)
    monkeypatch.delenv("DISCOURSE_PUBLIC_URL", raising=False)
    monkeypatch.delenv("GHOST_REAL_ADMIN_API_KEY", raising=False)
    settings = Settings.from_env()
    assert settings.discourse_base_url == "http://discourse:3000"
    assert settings.discourse_public_url == "http://127.0.0.1:3000"
    assert server_connection("ghost", {"url": "http://ghost:2368"})["admin_api_key"] == "dotenv-ghost-key"

    monkeypatch.setenv("DISCOURSE_BASE_URL", "http://process-env:3000")
    assert Settings.from_env().discourse_base_url == "http://process-env:3000"


def test_env_example_has_no_platform_credentials():
    from pathlib import Path

    names = {
        "GHOST_REAL_ADMIN_API_KEY",
        "DEVTO_REAL_API_KEY",
        "DEVTO_REAL_USERNAME",
        "DISCOURSE_REAL_API_KEY",
        "DISCOURSE_REAL_API_USERNAME",
        "LEMMY_REAL_USERNAME",
        "LEMMY_REAL_PASSWORD",
    }
    lines = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8").splitlines()
    configured_names = [
        line.split("=", 1)[0]
        for line in lines
        if "=" in line and line.split("=", 1)[0] in names and line.split("=", 1)[1].strip()
    ]
    assert not configured_names, "Credential values belong in the ignored runtime .env file."


def test_explicit_destination_preview_uses_existing_orchestrator_and_readiness_gate(client):
    destination = _destination(client, "ghost", "post", "https://ghost.example")
    response = client.post(
        "/api/v1/publishing-orchestrations/preview",
        headers={"Idempotency-Key": "ui-preview-smoke"},
        json={
            "content_item": {"title": "Preview smoke", "body": "No external post is requested."},
            "destination_ids": [destination["id"]],
            "dispatch": False,
        },
    )
    assert response.status_code == 200, response.text
    outcome = response.json()["destinations"][0]
    assert outcome["destination_id"] == destination["id"]
    assert outcome["status"] == "skipped"
    assert outcome["error_code"] == "CONNECTOR_NOT_READY"


@pytest.mark.parametrize(
    ("connector", "surface", "url"),
    [
        ("ghost", "post", "https://ghost.example.test/"),
        ("devto", "article", "https://dev.to/api"),
        ("discourse", "topic", "https://discourse.example.test/"),
        ("lemmy", "community_post", "https://lemmy.example.test/"),
    ],
)
def test_l0_mock_destination_dispatch_is_blocked_before_connector_publish(client, app, connector, surface, url, monkeypatch):
    assert app.state.settings.simulation_mode is True
    destination = _destination(client, connector, surface, url)
    assert destination["level"] == "L0"
    assert destination["last_certified_at"] is None
    connector_impl = app.state.connector_registry.get(connector)
    publish = MagicMock(side_effect=AssertionError("L0 destination must not reach publish"))
    monkeypatch.setattr(connector_impl, "publish", publish)
    connections = {destination["id"]: {"community_id": "17"}} if connector == "lemmy" else {}

    response = client.post(
        "/api/v1/publishing-orchestrations",
        headers={"Idempotency-Key": f"l0-mock-dispatch-{connector}"},
        json={
            "content_item": {"title": "Mock dispatch gate", "body": "No external publication is requested."},
            "destination_ids": [destination["id"]],
            "connections": connections,
            "dispatch": True,
        },
    )
    assert response.status_code == 202, response.text
    job = _wait_for_job(client, response.json()["job_id"])
    outcome = job["result"]["orchestration"]["destinations"][0]
    assert outcome["error_code"] == "CONNECTOR_NOT_READY"
    assert outcome["readiness"] == {
        "connector_key": connector,
        "publish_capable": True,
        "certification_level": "L0",
        "last_certified_at": None,
    }
    publish.assert_not_called()
