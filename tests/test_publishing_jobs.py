import time
from dataclasses import replace

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import RuleDecision
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
    response = client.post("/api/v1/destinations", json=payload)
    assert response.status_code == 201
    return response.json()["id"]


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


def test_required_idempotency_key_and_connector_disablement(settings):
    app = create_app(settings=replace(settings, connector_disabled=frozenset({"noop"})))
    with TestClient(app) as client:
        destination = create_destination(client, "noop")
        missing = client.post("/api/v1/publishing-jobs", json={"content_item": {"body": "x"}, "destination_ids": [destination]})
        assert missing.status_code == 400
        assert missing.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

        response = submit(client, "publish-disabled-1", [destination])
        job = wait_for_job(client, response.json()["job_id"])
        assert job["status"] == "failed"
        assert job["result"]["destinations"][0]["error_code"] == "CONNECTOR_DISABLED"
