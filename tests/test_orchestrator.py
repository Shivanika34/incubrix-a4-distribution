"""Comprehensive tests for the end-to-end multi-destination publishing orchestrator."""

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock
import pytest
from sqlalchemy import select

from app.database import Destination, RuleDecision
from app.errors import AppError


def wait_for_job(client, job_id, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200
        job = response.json()
        if job["status"] in {"succeeded", "partial", "failed"}:
            return job
        time.sleep(0.01)
    raise AssertionError(f"Orchestration job {job_id} did not complete within {timeout}s")


def create_test_destination(client, connector_key, *, certify=True, **overrides):
    payload = {
        "name": f"{connector_key} test destination",
        "url": "https://destination.example.test/post",
        "platform_key": connector_key,
        "surface": "post",
        "connector_key": connector_key,
        "category": "Tech",
        "audience_tags": ["python", "tech"],
        "language": "en",
        "automation_allowed": True,
        "self_promo_allowed": True,
        "link_policy": "allow",
        "level": "L3" if certify and connector_key != "assisted" else "L0",
        **overrides,
    }
    response = client.post("/api/v1/destinations", headers={"Idempotency-Key": f"create-{payload['name']}"}, json=payload)
    assert response.status_code == 201
    destination_id = response.json()["id"]
    if certify and connector_key != "assisted":
        with client.app.state.session_factory.begin() as session:
            destination = session.get(Destination, destination_id)
            destination.last_certified_at = datetime.now(timezone.utc)
    return destination_id


# 1. Preview Mode Tests (Zero publication calls)
def test_preview_mode_executes_zero_publish_calls(client, app, monkeypatch):
    dest_id = create_test_destination(client, "noop", name="Preview Destination")

    noop_connector = app.state.connector_registry.get("noop")
    publish_mock = MagicMock(side_effect=AssertionError("publish() must NOT be called in preview mode"))
    monkeypatch.setattr(noop_connector, "publish", publish_mock)

    payload = {
        "content_item": {
            "title": "Preview Title",
            "body": "Preview Body Content",
        },
        "destination_ids": [dest_id],
        "dispatch": False,
    }

    response = client.post(
        "/api/v1/publishing-orchestrations",
        headers={"Idempotency-Key": "test-preview-key-1"},
        json=payload,
    )
    assert response.status_code == 202
    job = wait_for_job(client, response.json()["job_id"])

    assert job["status"] == "succeeded"
    report = job["result"]["orchestration"]
    assert report["mode"] == "preview"
    assert report["dispatch_enabled"] is False
    assert report["totals"]["preview"] == 1
    assert report["totals"]["published"] == 0
    assert report["destinations"][0]["status"] == "preview"
    assert report["destinations"][0]["ready_to_publish"] is True
    assert publish_mock.call_count == 0


# 2. Synchronous Preview Endpoint
def test_synchronous_preview_endpoint(client):
    dest_id = create_test_destination(client, "noop", name="Sync Preview Destination")

    payload = {
        "content_item": {
            "title": "Sync Preview",
            "body": "Testing synchronous preview endpoint",
        },
        "destination_ids": [dest_id],
    }
    response = client.post(
        "/api/v1/publishing-orchestrations/preview",
        headers={"Idempotency-Key": "test-sync-preview-key"},
        json=payload,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "preview"
    assert data["totals"]["preview"] == 1
    assert data["destinations"][0]["status"] == "preview"
    assert data["destinations"][0]["tailored_content"]["title"] == "Sync Preview"


# 3. Explicit Destination Selection with Dispatch
def test_explicit_destination_dispatch(client):
    dest1 = create_test_destination(client, "noop", name="Dest 1")
    dest2 = create_test_destination(client, "noop", name="Dest 2")

    payload = {
        "content_item": {
            "title": "Dispatch Post",
            "body": "Full body text for dispatch testing",
        },
        "destination_ids": [dest1, dest2],
        "dispatch": True,
    }

    response = client.post(
        "/api/v1/publishing-orchestrations",
        headers={"Idempotency-Key": "test-dispatch-key-1"},
        json=payload,
    )
    assert response.status_code == 202
    job = wait_for_job(client, response.json()["job_id"])

    assert job["status"] == "succeeded"
    report = job["result"]["orchestration"]
    assert report["mode"] == "dispatch"
    assert report["selection_basis"] == "explicit"
    assert report["totals"]["published"] == 2
    assert report["destinations"][0]["status"] == "published"
    assert report["destinations"][0]["platform_post_id"] is not None
    assert report["destinations"][1]["status"] == "published"


# 4. Recommender-Based Destination Selection
def test_recommender_based_selection(client):
    dest_match = create_test_destination(
        client, "noop", name="AI Tech Forum", category="Tech", audience_tags=["python", "ai"]
    )
    dest_other = create_test_destination(
        client, "noop", name="Gardening Club", category="Gardening", audience_tags=["plants"]
    )

    payload = {
        "content_item": {
            "title": "Python AI Advances",
            "body": "New models for Python and AI workflows.",
            "hashtags": ["python", "ai"],
        },
        "recommendation": {
            "topic": "Tech",
            "audience_tags": ["python", "ai"],
            "top_k": 5,
            "min_score": 10.0,
        },
        "dispatch": True,
    }

    response = client.post(
        "/api/v1/publishing-orchestrations",
        headers={"Idempotency-Key": "test-recommender-sel-1"},
        json=payload,
    )
    assert response.status_code == 202
    job = wait_for_job(client, response.json()["job_id"])

    report = job["result"]["orchestration"]
    assert report["selection_basis"] == "recommendation"
    dest_ids = [d["destination_id"] for d in report["destinations"]]
    assert dest_match in dest_ids
    match_outcome = next(d for d in report["destinations"] if d["destination_id"] == dest_match)
    assert match_outcome["recommendation_score"] is not None
    assert match_outcome["recommendation_score"] > 0
    assert match_outcome["selection_basis"] == "recommendation"


# 5. Tailoring Adapts Content Per Destination
def test_destination_tailoring_per_destination(client):
    dest_forum = create_test_destination(
        client, "noop", name="Discussion Community", surface="topic", category="Tech"
    )
    dest_blog = create_test_destination(
        client, "noop", name="Tech Blog", surface="article", category="Tech"
    )

    payload = {
        "content_item": {
            "title": "Exploring Architecture Patterns",
            "body": "Here are detailed insights on scalable architectures.",
            "hashtags": ["architecture", "scaling"],
        },
        "destination_ids": [dest_forum, dest_blog],
        "dispatch": False,
    }

    response = client.post(
        "/api/v1/publishing-orchestrations",
        headers={"Idempotency-Key": "test-tailoring-diff-1"},
        json=payload,
    )
    job = wait_for_job(client, response.json()["job_id"])
    report = job["result"]["orchestration"]

    forum_outcome = next(d for d in report["destinations"] if d["destination_id"] == dest_forum)
    blog_outcome = next(d for d in report["destinations"] if d["destination_id"] == dest_blog)

    assert forum_outcome["tailored_content"] is not None
    assert blog_outcome["tailored_content"] is not None
    assert forum_outcome["applied_transformations"] is not None
    assert forum_outcome["tailored_content"]["body"] != blog_outcome["tailored_content"]["body"]


# 6. Posting Rule Safety: Recommendation Score Never Overrides Prohibitions
def test_rules_block_promotional_content_even_with_high_relevance(client, app, monkeypatch):
    blocked_dest = create_test_destination(
        client,
        "noop",
        name="Strict Technical Community",
        self_promo_allowed=False,
        category="Tech",
        audience_tags=["tech"],
    )
    publish_mock = MagicMock(side_effect=AssertionError("rule-blocked destination must not publish"))
    monkeypatch.setattr(app.state.connector_registry.get("noop"), "publish", publish_mock)

    payload = {
        "content_item": {
            "title": "Buy Our Developer Tool Today",
            "body": "Special limited discount on our new tool for tech developers.",
            "is_promotional": True,
        },
        "destination_ids": [blocked_dest],
        "dispatch": True,
    }

    response = client.post(
        "/api/v1/publishing-orchestrations",
        headers={"Idempotency-Key": "test-promo-blocked-1"},
        json=payload,
    )
    job = wait_for_job(client, response.json()["job_id"])
    report = job["result"]["orchestration"]

    assert job["status"] == "failed"
    assert report["totals"]["blocked"] == 1
    assert report["totals"]["published"] == 0
    outcome = report["destinations"][0]
    assert outcome["status"] == "blocked"
    assert outcome["error_code"] == "DEST_SPEC_VIOLATION"
    assert "self-promotion" in outcome["reason"].lower()

    # Verify decision logged in RuleDecision table
    with app.state.session_factory() as session:
        decision = session.scalar(
            select(RuleDecision).where(RuleDecision.destination_id == blocked_dest)
        )
        assert decision is not None
        assert decision.outcome == "blocked"
    assert publish_mock.call_count == 0


# 7. Assisted Destinations Produce Posting Pack Without Publishing
def test_assisted_destination_produces_pack_without_publishing(client):
    assisted_dest = create_test_destination(
        client,
        "assisted",
        name="Manual Submissions Portal",
        submit_url="https://destination.example.test/manual-submit",
    )

    payload = {
        "content_item": {
            "title": "Manual Submission Title",
            "body": "Content needing review and manual posting.",
        },
        "destination_ids": [assisted_dest],
        "dispatch": True,
    }

    response = client.post(
        "/api/v1/publishing-orchestrations",
        headers={"Idempotency-Key": "test-assisted-pack-1"},
        json=payload,
    )
    job = wait_for_job(client, response.json()["job_id"])
    report = job["result"]["orchestration"]

    assert job["status"] == "succeeded"
    assert report["totals"]["assisted"] == 1
    outcome = report["destinations"][0]
    assert outcome["status"] == "assisted"
    assert outcome["posting_pack"] is not None
    assert outcome["posting_pack"]["deep_link"] == "https://destination.example.test/manual-submit"
    assert outcome["platform_post_id"] is None


def test_assisted_destination_does_not_bypass_posting_rules(client, app):
    assisted_dest = create_test_destination(
        client,
        "assisted",
        name="Manual But Strict",
        self_promo_allowed=False,
    )
    response = client.post(
        "/api/v1/publishing-orchestrations",
        headers={"Idempotency-Key": "test-assisted-rule-block"},
        json={
            "content_item": {
                "title": "Promotional offer",
                "body": "Buy the product now.",
                "is_promotional": True,
            },
            "destination_ids": [assisted_dest],
            "dispatch": True,
        },
    )
    job = wait_for_job(client, response.json()["job_id"])
    outcome = job["result"]["orchestration"]["destinations"][0]

    assert job["status"] == "failed"
    assert outcome["status"] == "blocked"
    assert outcome["error_code"] == "DEST_SPEC_VIOLATION"
    assert "self-promotion" in outcome["reason"].lower()
    assert "posting_pack" not in outcome


# 8. Disabled Connector is Excluded Safely
def test_disabled_connector_is_excluded(client, app, monkeypatch):
    dest_id = create_test_destination(client, "noop", name="Disabled Destination")

    monkeypatch.setattr(app.state, "disabled_connectors", frozenset({"noop"}))
    monkeypatch.setattr(app.state.jobs, "disabled_connectors", frozenset({"noop"}))

    payload = {
        "content_item": {"title": "Test Title", "body": "Test Body"},
        "destination_ids": [dest_id],
        "dispatch": True,
    }

    response = client.post(
        "/api/v1/publishing-orchestrations",
        headers={"Idempotency-Key": "test-disabled-connector-1"},
        json=payload,
    )
    job = wait_for_job(client, response.json()["job_id"])
    report = job["result"]["orchestration"]

    assert job["status"] == "failed"
    assert report["totals"]["skipped"] == 1
    outcome = report["destinations"][0]
    assert outcome["status"] == "skipped"
    assert outcome["error_code"] == "CONNECTOR_DISABLED"


def test_uncertified_destination_never_reaches_publish(client, app, monkeypatch):
    dest_id = create_test_destination(client, "noop", certify=False, name="Uncertified")
    connector = app.state.connector_registry.get("noop")
    publish_mock = MagicMock(side_effect=AssertionError("uncertified destinations must not publish"))
    monkeypatch.setattr(connector, "publish", publish_mock)

    response = client.post(
        "/api/v1/publishing-orchestrations",
        headers={"Idempotency-Key": "test-uncertified-key"},
        json={
            "content_item": {"title": "A title", "body": "A body"},
            "destination_ids": [dest_id],
            "dispatch": True,
        },
    )
    job = wait_for_job(client, response.json()["job_id"])
    outcome = job["result"]["orchestration"]["destinations"][0]
    assert job["status"] == "failed"
    assert outcome["status"] == "skipped"
    assert outcome["error_code"] == "CONNECTOR_NOT_READY"
    assert publish_mock.call_count == 0


def test_sync_preview_requires_idempotency_key(client):
    response = client.post(
        "/api/v1/publishing-orchestrations/preview",
        json={"content_item": {"title": "Title", "body": "Body"}, "destination_ids": ["missing"]},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_VALIDATION_ERROR"


def test_frequency_cap_uses_prior_confirmed_publications(client, app):
    dest_id = create_test_destination(
        client,
        "noop",
        name="Frequency Capped",
        frequency_cap={"max_posts": 1, "window_seconds": 3600},
    )
    from app.database import Job
    from uuid import uuid4

    with app.state.session_factory.begin() as session:
        session.add(Job(
            id=str(uuid4()),
            job_type="publishing",
            status="succeeded",
            progress=100,
            result_links=[],
            result={"destinations": [{"destination_id": dest_id, "status": "published"}]},
        ))

    response = client.post(
        "/api/v1/publishing-orchestrations",
        headers={"Idempotency-Key": "test-frequency-cap"},
        json={
            "content_item": {"title": "Another post", "body": "This should wait."},
            "destination_ids": [dest_id],
            "dispatch": True,
        },
    )
    job = wait_for_job(client, response.json()["job_id"])
    outcome = job["result"]["orchestration"]["destinations"][0]
    assert job["status"] == "failed", outcome
    assert outcome["status"] == "blocked", outcome
    assert outcome["error_code"] == "DEST_RULE_FREQUENCY_CAP"


def test_duplicate_dispatch_request_does_not_publish_twice(client, app, monkeypatch):
    dest_id = create_test_destination(client, "noop", name="Idempotent Dispatch")
    connector = app.state.connector_registry.get("noop")
    publish_mock = MagicMock(return_value={"platform_post_id": "confirmed-post"})
    monkeypatch.setattr(connector, "publish", publish_mock)
    payload = {
        "content_item": {"title": "Once", "body": "Only one publication."},
        "destination_ids": [dest_id],
        "dispatch": True,
    }
    headers = {"Idempotency-Key": "test-dispatch-replay"}

    first = client.post("/api/v1/publishing-orchestrations", headers=headers, json=payload)
    completed = wait_for_job(client, first.json()["job_id"])
    replay = client.post("/api/v1/publishing-orchestrations", headers=headers, json=payload)

    assert replay.status_code == 202
    assert replay.json()["job_id"] == first.json()["job_id"]
    assert completed["result"]["orchestration"]["totals"]["published"] == 1
    assert publish_mock.call_count == 1


# 9. Partial Success Across Multiple Destinations
def test_partial_success_mix(client):
    dest_ok = create_test_destination(client, "noop", name="Will Publish")
    dest_block = create_test_destination(
        client, "noop", name="Will Block", self_promo_allowed=False
    )
    dest_assisted = create_test_destination(client, "assisted", name="Will Assist")

    payload = {
        "content_item": {
            "title": "Sale Promo",
            "body": "Special sale offer for developers.",
            "is_promotional": True,
        },
        "destination_ids": [dest_ok, dest_block, dest_assisted],
        "dispatch": True,
    }

    response = client.post(
        "/api/v1/publishing-orchestrations",
        headers={"Idempotency-Key": "test-partial-mix-1"},
        json=payload,
    )
    job = wait_for_job(client, response.json()["job_id"])
    report = job["result"]["orchestration"]

    assert job["status"] == "partial"
    assert report["totals"]["published"] == 1
    assert report["totals"]["blocked"] == 1
    assert report["totals"]["assisted"] == 1

    outcomes = {d["destination_id"]: d for d in report["destinations"]}
    assert outcomes[dest_ok]["status"] == "published"
    assert outcomes[dest_block]["status"] == "blocked"
    assert outcomes[dest_assisted]["status"] == "assisted"


# 10. Uncertain Publication Outcome Handled Safely
def test_uncertain_publication_outcome_does_not_count_as_success(client, app, monkeypatch):
    dest_id = create_test_destination(client, "noop", name="Timeout Destination")

    noop_connector = app.state.connector_registry.get("noop")

    def mock_publish_timeout(*args, **kwargs):
        raise AppError("PUBLISH_OUTCOME_UNKNOWN", "Platform timed out after request was sent.", True)

    monkeypatch.setattr(noop_connector, "publish", mock_publish_timeout)

    payload = {
        "content_item": {"title": "Test Title", "body": "Body content"},
        "destination_ids": [dest_id],
        "dispatch": True,
    }

    response = client.post(
        "/api/v1/publishing-orchestrations",
        headers={"Idempotency-Key": "test-unknown-outcome-1"},
        json=payload,
    )
    job = wait_for_job(client, response.json()["job_id"])
    report = job["result"]["orchestration"]

    assert job["status"] == "partial"  # Uncertain outcomes are flagged as partial for operator attention
    assert report["totals"]["outcome_unknown"] == 1
    assert report["totals"]["published"] == 0
    outcome = report["destinations"][0]
    assert outcome["status"] == "outcome_unknown"
    assert outcome["error_code"] == "PUBLISH_OUTCOME_UNKNOWN"


def test_total_publication_failure_is_not_reported_as_success(client, app, monkeypatch):
    dest_id = create_test_destination(client, "noop", name="Rejecting Destination")
    connector = app.state.connector_registry.get("noop")
    publish_mock = MagicMock(side_effect=AppError(
        "PLATFORM_CONTENT_REJECTED", "The platform rejected the post.", False
    ))
    monkeypatch.setattr(connector, "publish", publish_mock)

    response = client.post(
        "/api/v1/publishing-orchestrations",
        headers={"Idempotency-Key": "test-total-publish-failure"},
        json={
            "content_item": {"title": "Rejected", "body": "This cannot be posted."},
            "destination_ids": [dest_id],
            "dispatch": True,
        },
    )
    job = wait_for_job(client, response.json()["job_id"])
    report = job["result"]["orchestration"]

    assert job["status"] == "failed"
    assert report["totals"]["failed"] == 1
    assert report["totals"]["published"] == 0
    assert report["destinations"][0]["status"] == "failed"
    assert publish_mock.call_count == 1


def test_missing_publication_evidence_is_uncertain_not_success(client, app, monkeypatch):
    dest_id = create_test_destination(client, "noop", name="Missing Evidence")
    connector = app.state.connector_registry.get("noop")
    publish_mock = MagicMock(return_value={})
    monkeypatch.setattr(connector, "publish", publish_mock)
    response = client.post(
        "/api/v1/publishing-orchestrations",
        headers={"Idempotency-Key": "test-missing-publish-evidence"},
        json={
            "content_item": {"title": "No evidence", "body": "Connector did not return an ID."},
            "destination_ids": [dest_id],
            "dispatch": True,
        },
    )
    job = wait_for_job(client, response.json()["job_id"])
    report = job["result"]["orchestration"]

    assert job["status"] == "partial"
    assert report["totals"]["published"] == 0
    assert report["totals"]["outcome_unknown"] == 1
    assert report["destinations"][0]["status"] == "outcome_unknown"
    assert publish_mock.call_count == 1


# 11. Idempotency Key Handling
def test_idempotency_key_validation_and_caching(client):
    dest_id = create_test_destination(client, "noop", name="Idempotency Destination")

    payload = {
        "content_item": {"title": "Original Title", "body": "Original Body"},
        "destination_ids": [dest_id],
        "dispatch": False,
    }

    # Missing Idempotency-Key
    res_no_key = client.post("/api/v1/publishing-orchestrations", json=payload)
    assert res_no_key.status_code == 422
    assert res_no_key.json()["error"]["code"] == "REQUEST_VALIDATION_ERROR"

    # First request
    key = "test-idempotency-key-orchestration-1"
    res1 = client.post("/api/v1/publishing-orchestrations", headers={"Idempotency-Key": key}, json=payload)
    assert res1.status_code == 202
    job_id_1 = res1.json()["job_id"]

    # Replay identical request
    res2 = client.post("/api/v1/publishing-orchestrations", headers={"Idempotency-Key": key}, json=payload)
    assert res2.status_code == 202
    job_id_2 = res2.json()["job_id"]
    assert job_id_1 == job_id_2

    # Reused key with different payload
    diff_payload = {**payload, "content_item": {"title": "Different Title", "body": "Different Body"}}
    res3 = client.post("/api/v1/publishing-orchestrations", headers={"Idempotency-Key": key}, json=diff_payload)
    assert res3.status_code == 409
    assert res3.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
