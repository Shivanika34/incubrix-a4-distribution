import time
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import RuleDecision
from app.errors import AppError
from app.main import create_app


def wait_for_job(client, job_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200
        job = response.json()
        if job["status"] in {"succeeded", "partial", "failed"}:
            return job
        time.sleep(0.01)
    raise AssertionError("publishing job did not finish before the test deadline")


def create_destination(client, connector_key, **overrides):
    payload = {
        "name": f"{connector_key} destination",
        "url": "https://destination.example.test/submit",
        "platform_key": connector_key,
        "surface": "post",
        "connector_key": connector_key,
        **overrides,
    }
    response = client.post("/api/v1/destinations", headers={"Idempotency-Key": f"create-{connector_key}-{payload['name']}"}, json=payload)
    assert response.status_code == 201
    destination_id = response.json()["id"]
    from app.database import Destination
    with client.app.state.session_factory.begin() as session:
        destination = session.get(Destination, destination_id)
        destination.level = "L3"
        destination.last_certified_at = datetime.now(timezone.utc)
    return destination_id


def submit(client, key, destination_ids, content_item=None, connections=None):
    return client.post(
        "/api/v1/publishing-jobs",
        headers={"Idempotency-Key": key},
        json={
            "content_item": content_item or {"title": "Hello", "body": "A post"},
            "destination_ids": destination_ids,
            "connections": connections or {},
        },
    )


def test_successful_multi_destination_publish_and_polling(client):
    first = create_destination(client, "noop", name="First")
    second = create_destination(client, "noop", name="Second")

    response = submit(client, "publish-multi-1", [first, second])
    assert response.status_code == 202
    job = wait_for_job(client, response.json()["job_id"])

    assert job["status"] == "succeeded"
    assert job["progress"] == 100
    assert [item["status"] for item in job["result"]["destinations"]] == ["published", "published"]
    assert all(item["platform_post_id"] for item in job["result"]["destinations"])


def test_rules_block_one_destination_without_aborting_other_publish(client, app):
    blocked = create_destination(client, "noop", name="Blocked", self_promo_allowed=False)
    allowed = create_destination(client, "noop", name="Allowed")

    response = submit(
        client,
        "publish-rules-1",
        [blocked, allowed],
        {"title": "Promotion", "body": "Buy this", "is_promotional": True},
    )
    job = wait_for_job(client, response.json()["job_id"])
    outcomes = {item["destination_id"]: item for item in job["result"]["destinations"]}

    assert job["status"] == "partial"
    assert outcomes[blocked]["error_code"] == "DEST_SPEC_VIOLATION"
    assert outcomes[allowed]["status"] == "published"
    with app.state.session_factory() as session:
        assert session.scalar(select(RuleDecision).where(RuleDecision.destination_id == blocked)) is not None


def test_connector_error_is_stored_per_destination(client):
    webhook = create_destination(client, "webhook")
    noop = create_destination(client, "noop")

    response = submit(client, "publish-error-1", [webhook, noop])
    job = wait_for_job(client, response.json()["job_id"])
    outcomes = {item["destination_id"]: item for item in job["result"]["destinations"]}

    assert job["status"] == "partial"
    assert outcomes[webhook]["error_code"] == "CONNECTION_AUTH_EXPIRED"
    assert outcomes[noop]["status"] == "published"


def test_assisted_connector_generates_pack_without_publishing(client):
    destination = create_destination(client, "assisted", submit_url="https://destination.example.test/submit")

    response = submit(client, "publish-assisted-1", [destination])
    job = wait_for_job(client, response.json()["job_id"])
    outcome = job["result"]["destinations"][0]

    assert job["status"] == "succeeded"
    assert outcome["status"] == "assisted"
    assert outcome["posting_pack"]["deep_link"] == "https://destination.example.test/submit"
    assert outcome["platform_post_id"] is None


def test_assisted_destination_setting_generates_pack_instead_of_auto_publish(client):
    destination = create_destination(client, "assisted", automation_allowed=False)

    response = submit(client, "publish-assisted-setting-1", [destination])
    job = wait_for_job(client, response.json()["job_id"])

    assert job["status"] == "succeeded"
    assert job["result"]["destinations"][0]["status"] == "assisted"


def test_duplicate_publishing_request_returns_same_job(client, app):
    destination = create_destination(client, "noop")
    first = submit(client, "publish-duplicate-1", [destination])
    second = submit(client, "publish-duplicate-1", [destination])

    assert first.status_code == second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    wait_for_job(client, first.json()["job_id"])
    assert len(app.state.connector_registry.get("noop").posts) == 1


def test_timeout_is_outcome_unknown_and_mixed_with_confirmed_results(client, app, monkeypatch):
    successful_destination = create_destination(client, "noop")
    uncertain_destination = create_destination(client, "noop")
    failed_destination = create_destination(client, "webhook")
    connector = app.state.connector_registry.get("noop")
    original_publish = connector.publish

    def mixed_publish(rendition, destination, connection, idempotency_key):
        if destination["id"] == uncertain_destination:
            raise AppError("PUBLISH_OUTCOME_UNKNOWN", "The publication request timed out after it may have reached the platform. Check the platform before retrying.", True)
        return original_publish(rendition, destination, connection, idempotency_key)

    monkeypatch.setattr(connector, "publish", mixed_publish)
    response = submit(client, "publish-unknown-mixed", [successful_destination, uncertain_destination, failed_destination])
    job = wait_for_job(client, response.json()["job_id"])
    outcomes = {item["destination_id"]: item for item in job["result"]["destinations"]}
    assert job["status"] == "partial"
    assert outcomes[successful_destination]["status"] == "published"
    assert outcomes[uncertain_destination]["status"] == "outcome_unknown"
    assert outcomes[uncertain_destination]["error_code"] == "PUBLISH_OUTCOME_UNKNOWN"
    assert outcomes[failed_destination]["status"] == "failed"


def test_only_uncertain_destinations_are_not_reported_as_definite_failure(client, app, monkeypatch):
    destination = create_destination(client, "noop")

    def uncertain(*args, **kwargs):
        raise AppError(
            "PUBLISH_OUTCOME_UNKNOWN",
            "The publication request timed out after it may have reached the platform. Check the platform before retrying.",
            True,
        )

    monkeypatch.setattr(app.state.connector_registry.get("noop"), "publish", uncertain)
    response = submit(client, "publish-unknown-only", [destination])
    job = wait_for_job(client, response.json()["job_id"])
    assert job["status"] == "partial"
    assert job["result"]["destinations"][0]["status"] == "outcome_unknown"


def test_normal_publishing_api_enforces_frequency_cap_from_confirmed_history(client, app):
    destination = create_destination(
        client, "noop", frequency_cap={"max_posts": 1, "window_seconds": 3600}
    )
    first = submit(client, "normal-frequency-first", [destination])
    first_job = wait_for_job(client, first.json()["job_id"])
    assert first_job["result"]["destinations"][0]["status"] == "published"

    second = submit(client, "normal-frequency-second", [destination])
    second_job = wait_for_job(client, second.json()["job_id"])
    outcome = second_job["result"]["destinations"][0]
    assert second_job["status"] == "failed"
    assert outcome["error_code"] == "DEST_RULE_FREQUENCY_CAP"


def test_normal_publishing_api_counts_confirmed_orchestration_history(client, app):
    destination = create_destination(
        client,
        "noop",
        name="Shared frequency history",
        frequency_cap={"max_posts": 1, "window_seconds": 3600},
    )
    from app.database import Destination
    with app.state.session_factory.begin() as session:
        record = session.get(Destination, destination)
        record.level = "L3"
        record.last_certified_at = datetime.now(timezone.utc)

    orchestration = client.post(
        "/api/v1/publishing-orchestrations",
        headers={"Idempotency-Key": "shared-frequency-orchestration"},
        json={
            "content_item": {"title": "First", "body": "Confirmed by orchestration."},
            "destination_ids": [destination],
            "dispatch": True,
        },
    )
    assert orchestration.status_code == 202
    orchestrated_job = wait_for_job(client, orchestration.json()["job_id"])
    assert orchestrated_job["result"]["orchestration"]["destinations"][0]["status"] == "published"

    next_post = submit(client, "shared-frequency-publishing", [destination])
    publishing_job = wait_for_job(client, next_post.json()["job_id"])
    outcome = publishing_job["result"]["destinations"][0]
    assert publishing_job["status"] == "failed"
    assert outcome["status"] == "failed"
    assert outcome["error_code"] == "DEST_RULE_FREQUENCY_CAP"
    assert len(app.state.connector_registry.get("noop").posts) == 1


def test_timeout_after_mock_acceptance_and_same_key_replay_never_dispatches_again(client, app, monkeypatch):
    destination = create_destination(client, "noop")
    connector = app.state.connector_registry.get("noop")
    accepted_posts = []
    original_publish = connector.publish

    def accept_then_lose_confirmation(rendition, destination_data, connection, key):
        accepted_posts.append(original_publish(rendition, destination_data, connection, key))
        raise AppError(
            "PUBLISH_OUTCOME_UNKNOWN",
            "Mock accepted the post but the confirmation timed out.",
            True,
        )

    monkeypatch.setattr(connector, "publish", accept_then_lose_confirmation)
    payload = {"content_item": {"title": "May be accepted", "body": "Timeout case"}, "destination_ids": [destination]}
    headers = {"Idempotency-Key": "timeout-accepted-replay"}
    first = client.post("/api/v1/publishing-jobs", headers=headers, json=payload)
    first_job = wait_for_job(client, first.json()["job_id"])
    assert first_job["result"]["destinations"][0]["status"] == "outcome_unknown"

    replay = client.post("/api/v1/publishing-jobs", headers=headers, json=payload)
    assert replay.status_code == 202
    assert replay.json()["job_id"] == first.json()["job_id"]
    assert len(accepted_posts) == 1
    assert len(connector.posts) == 1

    # The uncertain record is not counted as confirmed frequency-cap history.
    monkeypatch.setattr(connector, "publish", original_publish)
    followup = submit(client, "timeout-followup-new-key", [destination])
    followup_job = wait_for_job(client, followup.json()["job_id"])
    assert followup_job["result"]["destinations"][0]["status"] == "published"
    assert len(connector.posts) == 2


def test_required_idempotency_key_and_connector_disablement(settings):
    app = create_app(settings=replace(settings, connector_disabled=frozenset({"noop"})))
    with TestClient(app) as client:
        destination = create_destination(client, "noop")
        missing = client.post("/api/v1/publishing-jobs", json={"content_item": {"body": "x"}, "destination_ids": [destination]})
        assert missing.status_code == 422
        assert missing.json()["error"]["code"] == "REQUEST_VALIDATION_ERROR"

        response = submit(client, "publish-disabled-1", [destination])
        job = wait_for_job(client, response.json()["job_id"])
        assert job["status"] == "failed"
        assert job["result"]["destinations"][0]["error_code"] == "CONNECTOR_DISABLED"


def test_uncertified_destination_never_reaches_standard_publish(client, app, monkeypatch):
    destination_id = create_destination(client, "noop", name="Uncertified standard destination")
    from app.database import Destination
    with app.state.session_factory.begin() as session:
        destination = session.get(Destination, destination_id)
        destination.level = "L0"
        destination.last_certified_at = None

    connector = app.state.connector_registry.get("noop")
    publish_mock = MagicMock(side_effect=AssertionError("uncertified connector must not publish"))
    monkeypatch.setattr(connector, "publish", publish_mock)
    response = submit(client, "publishing-uncertified", [destination_id])
    job = wait_for_job(client, response.json()["job_id"])
    outcome = job["result"]["destinations"][0]
    assert job["status"] == "failed"
    assert outcome["error_code"] == "CONNECTOR_NOT_READY"
    assert outcome["readiness"]["certification_level"] == "L0"
    publish_mock.assert_not_called()


def test_uncertified_live_destination_never_reaches_standard_publish(settings, monkeypatch):
    """Live mode retains the L3/L4 gate; no platform network call is made here."""
    app = create_app(settings=replace(settings, simulation_mode=False))
    with TestClient(app) as live_client:
        created = live_client.post(
            "/api/v1/destinations",
            headers={"Idempotency-Key": "create-uncertified-live-destination"},
            json={
                "name": "Uncertified live destination",
                "url": "https://destination.example.test/submit",
                "platform_key": "noop",
                "surface": "post",
                "connector_key": "noop",
            },
        )
        assert created.status_code == 201, created.text
        destination_id = created.json()["id"]
        assert created.json()["level"] == "L0"
        assert created.json()["last_certified_at"] is None

        connector = app.state.connector_registry.get("noop")
        publish = MagicMock(side_effect=AssertionError("uncertified real destination must not publish"))
        monkeypatch.setattr(connector, "publish", publish)
        response = submit(live_client, "uncertified-live-destination", [destination_id])
        job = wait_for_job(live_client, response.json()["job_id"])
        outcome = job["result"]["destinations"][0]

        assert job["status"] == "failed"
        assert outcome["error_code"] == "CONNECTOR_NOT_READY"
        assert outcome["readiness"]["certification_level"] == "L0"
        publish.assert_not_called()
