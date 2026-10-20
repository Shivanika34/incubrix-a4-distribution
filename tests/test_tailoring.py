"""Offline unit and integration tests for destination-specific content tailoring."""

import copy
import time
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
import pytest

from app.database import Destination
from app.main import create_app
from app.tailoring import (
    TailoringService,
    compute_content_ref,
    format_hashtags,
    format_mentions,
    tailor_for_destination,
    truncate_at_word_boundary,
)


def create_destination(client, connector_key, **overrides):
    payload = {
        "name": f"{connector_key} destination",
        "url": "https://destination.example.test/submit",
        "platform_key": connector_key,
        "surface": "post",
        "connector_key": connector_key,
        **overrides,
    }
    response = client.post("/api/v1/destinations", headers={"Idempotency-Key": f"create-{payload['name']}"}, json=payload)
    assert response.status_code == 201
    return response.json()["id"]


def wait_for_job(client, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200
        job = response.json()
        if job["status"] in {"succeeded", "partial", "failed"}:
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish before test deadline")


# --- Test 1: Content already within limits ---
def test_content_already_within_limits():
    content = {
        "title": "Short Title",
        "body": "This is a body that easily fits within destination limits.",
    }
    dest = {
        "id": "dest-1",
        "connector_key": "noop",
        "platform_key": "noop",
        "surface": "post",
        "link_policy": "allow",
    }
    manifest = {
        "surfaces": [
            {"key": "post", "content_types": ["text"], "spec": {"title_max_chars": 120, "body_max_chars": 10000}}
        ]
    }
    result = tailor_for_destination(content, dest, manifest=manifest)
    assert result["ready_for_publishing"] is True
    assert result["tailored_content"]["title"] == "Short Title"
    assert result["tailored_content"]["body"] == content["body"]
    assert not any(t["type"] in {"title_truncation", "body_truncation"} for t in result["applied_transformations"])
    assert result["validation_errors"] == []


# --- Test 2: Title and body exceeding destination limits ---
def test_title_and_body_exceeding_destination_limits():
    content = {
        "title": "A very long title that exceeds the limit of twenty characters",
        "body": "A very long body word word word word word word that definitely exceeds fifty characters limit.",
    }
    dest = {
        "id": "dest-2",
        "connector_key": "noop",
        "surface": "post",
    }
    manifest = {
        "surfaces": [
            {"key": "post", "content_types": ["text"], "spec": {"title_max_chars": 25, "body_max_chars": 50}}
        ]
    }
    result = tailor_for_destination(content, dest, manifest=manifest)
    assert result["ready_for_publishing"] is True
    assert len(result["tailored_content"]["title"]) <= 25
    assert len(result["tailored_content"]["body"]) <= 50
    types = [t["type"] for t in result["applied_transformations"]]
    assert "title_truncation" in types
    assert "body_truncation" in types


# --- Test 3: Truncation at word boundaries ---
def test_truncation_at_word_boundaries():
    text = "The quick brown fox jumps over the lazy dog"
    # Limit to 20 chars; candidate before ellipsis (len 3) is 17 chars ("The quick brown f")
    # Last word boundary is after "brown", giving "The quick brown..." (len 18 <= 20)
    truncated, was_trunc, warn = truncate_at_word_boundary(text, 20)
    assert was_trunc is True
    assert len(truncated) <= 20
    assert truncated == "The quick brown..."
    # Ensure "fox" was not cut into "fo..." or "f..."
    assert "fox" not in truncated


# --- Test 4: Unicode and multilingual content ---
def test_unicode_and_multilingual_content():
    # Spanish with accents and emoji
    content = {
        "title": "Título en español con acentos y emoji 🚀",
        "body": "Contenido en español con caracteres especiales: á, é, í, ó, ú, ñ. Todo preservado.",
        "language": "es",
    }
    dest = {
        "id": "dest-es",
        "connector_key": "noop",
        "surface": "post",
        "language": "es",
    }
    manifest = {
        "surfaces": [
            {"key": "post", "content_types": ["text"], "spec": {"title_max_chars": 100, "body_max_chars": 200}}
        ]
    }
    result = tailor_for_destination(content, dest, manifest=manifest)
    assert result["ready_for_publishing"] is True
    assert "🚀" in result["tailored_content"]["title"]
    assert "ñ" in result["tailored_content"]["body"]
    assert result["tailored_content"]["language"] == "es"
    assert result["warnings"] == []

    # CJK text without spaces
    cjk_text = "日本語のタイトルと本文のテストです。長い文章が続く場合でも安全に切り詰められます。"
    cjk_trunc, was_trunc, _ = truncate_at_word_boundary(cjk_text, 15)
    assert was_trunc is True
    assert len(cjk_trunc) <= 15
    assert cjk_trunc.endswith("...")

    # Language mismatch warning without rewriting
    dest_de = {**dest, "language": "de"}
    res_mismatch = tailor_for_destination(content, dest_de, manifest=manifest)
    assert any("differs from destination expected language 'de'" in w for w in res_mismatch["warnings"])
    assert res_mismatch["tailored_content"]["language"] == "es"


# --- Test 5: Empty or whitespace-only content ---
def test_empty_or_whitespace_only_content():
    content = {
        "title": "   \t\n  ",
        "body": "",
    }
    dest = {"id": "dest-empty", "connector_key": "noop", "surface": "post"}
    manifest = {"surfaces": [{"key": "post", "content_types": ["text"], "spec": {}}]}

    result = tailor_for_destination(content, dest, manifest=manifest)
    assert result["ready_for_publishing"] is False
    assert "Title cannot be empty or whitespace only." in result["validation_errors"]
    assert "Body cannot be empty or whitespace only." in result["validation_errors"]


# --- Test 6: Discussion-first forum formatting ---
def test_discussion_first_forum_formatting():
    content = {
        "title": "Why SQLite is great for embedded jobs",
        "body": "We migrated our job queue to SQLite and simplified maintenance.",
    }
    dest = {
        "id": "dest-discourse",
        "connector_key": "discourse",
        "platform_key": "discourse",
        "surface": "topic",
    }
    manifest = {
        "surfaces": [
            {"key": "topic", "content_types": ["text"], "spec": {"title_max_chars": 255, "body_max_chars": 32000}}
        ]
    }
    result = tailor_for_destination(content, dest, manifest=manifest)
    assert result["ready_for_publishing"] is True
    assert any(t["type"] == "discussion_framing" for t in result["applied_transformations"])
    assert "What are your thoughts on this?" in result["tailored_content"]["body"]

    # When content already includes a discussion question, prompt is not duplicated
    content_with_q = {
        "title": "Has anyone tried SQLite in production?",
        "body": "What are your thoughts on using SQLite for job queues?",
    }
    res_existing_q = tailor_for_destination(content_with_q, dest, manifest=manifest)
    assert not any(t["type"] == "discussion_framing" for t in res_existing_q["applied_transformations"])


# --- Test 7: Article-style formatting ---
def test_article_style_formatting():
    title = "Advanced Connector Architecture"
    content = {
        "title": title,
        "body": f"# {title}\n\nIn this article, we explain how the connector registry works.\n\n\n\nSecond paragraph.",
        "content_type": "article",
    }
    dest = {
        "id": "dest-devto",
        "connector_key": "devto",
        "platform_key": "devto",
        "surface": "article",
    }
    manifest = {
        "surfaces": [
            {"key": "article", "content_types": ["text", "image"], "spec": {"title_max_chars": 128, "body_max_chars": 100000}}
        ]
    }
    result = tailor_for_destination(content, dest, manifest=manifest)
    assert result["ready_for_publishing"] is True
    # Duplicate H1 should be removed from body
    assert not result["tailored_content"]["body"].startswith(f"# {title}")
    assert result["tailored_content"]["body"].startswith("In this article")
    # Excessive blank lines normalized
    assert "\n\n\n" not in result["tailored_content"]["body"]
    assert any(t["type"] == "article_formatting" for t in result["applied_transformations"])


# --- Test 8: Canonical URL inclusion and link-policy rejection ---
def test_canonical_url_inclusion_and_link_policy_rejection():
    content = {
        "title": "Cross-posted Article",
        "body": "Original content with canonical reference.",
        "canonical_url": "https://creator.example.com/original-article",
    }

    # Case A: Platform supports canonical URL and links allowed (devto)
    dest_allowed = {
        "id": "dest-devto",
        "connector_key": "devto",
        "platform_key": "devto",
        "surface": "article",
        "link_policy": "allow",
    }
    manifest = {
        "surfaces": [
            {"key": "article", "content_types": ["text"], "spec": {"links_allowed": True}}
        ]
    }
    res_allowed = tailor_for_destination(content, dest_allowed, manifest=manifest)
    assert res_allowed["ready_for_publishing"] is True
    assert res_allowed["tailored_content"]["canonical_url"] == "https://creator.example.com/original-article"
    assert any(t["type"] == "canonical_url_inclusion" for t in res_allowed["applied_transformations"])

    # Case B: Destination blocks links
    dest_blocked = {
        "id": "dest-no-links",
        "connector_key": "noop",
        "surface": "post",
        "link_policy": "block",
    }
    res_blocked = tailor_for_destination(content, dest_blocked, manifest=manifest)
    assert res_blocked["ready_for_publishing"] is False
    assert any(r["transformation"] == "canonical_url_inclusion" for r in res_blocked["rejected_transformations"])
    assert "Destination does not allow links, but canonical URL was provided." in res_blocked["validation_errors"]
    assert res_blocked["tailored_content"]["canonical_url"] is None

    # Case C: Destination strips links
    dest_strip = {
        "id": "dest-strip",
        "connector_key": "noop",
        "surface": "post",
        "link_policy": "strip",
    }
    res_strip = tailor_for_destination(content, dest_strip, manifest=manifest)
    assert res_strip["ready_for_publishing"] is True
    assert res_strip["tailored_content"]["canonical_url"] is None
    assert any(t["type"] == "link_policy_strip" for t in res_strip["applied_transformations"])


# --- Test 9: Hashtag and mention formatting ---
def test_hashtag_and_mention_formatting():
    tags = ["#Python", "Machine Learning", "FastAPI", "#WebDev", "ExtraTag"]
    mentions = ["@alice", "bob"]

    # Dev.to: max 4 tags, lowercase alnum, no '#'
    devto_tags, devto_warn, devto_app = format_hashtags(tags, "devto")
    assert len(devto_tags) == 4
    assert devto_tags == ["python", "machinelearning", "fastapi", "webdev"]
    assert any("Dev.to supports at most 4 tags" in w for w in devto_warn)

    # Generic / forum: standardized with '#' prefix
    forum_tags, forum_warn, forum_app = format_hashtags(tags, "discourse")
    assert len(forum_tags) == 5
    assert forum_tags[0] == "#Python"
    assert forum_tags[1] == "#Machine_Learning"

    # Mentions standardized with '@' prefix
    formatted_mentions, mention_warn, _ = format_mentions(mentions, "discourse")
    assert formatted_mentions == ["@alice", "@bob"]


# --- Test 10: Unsupported media types ---
def test_unsupported_media_types():
    content = {
        "title": "Video Announcement",
        "body": "Here is the video walkthrough.",
        "content_type": "video_link",
        "files": [{"type": "video/mp4", "url": "https://example.com/video.mp4"}],
    }
    # Noop connector surface only supports "text"
    dest = {"id": "dest-noop", "connector_key": "noop", "surface": "post"}
    manifest = {
        "surfaces": [
            {"key": "post", "content_types": ["text"], "spec": {}}
        ]
    }
    result = tailor_for_destination(content, dest, manifest=manifest)
    assert result["ready_for_publishing"] is False
    assert any("Content type 'video_link' is not supported" in err for err in result["validation_errors"])
    assert any("Media files are not supported" in err for err in result["validation_errors"])
    assert any(r["transformation"] == "content_type_selection" for r in result["rejected_transformations"])


# --- Test 11: AI-disclosure requirements ---
def test_ai_disclosure_requirements():
    content_ai = {
        "title": "AI Generated Draft",
        "body": "This article was drafted with assistance.",
        "ai_generated": True,
    }
    dest_append = {
        "id": "dest-ai-app",
        "connector_key": "noop",
        "surface": "post",
        "disclosure_required": True,
        "disclosure_mode": "append",
    }
    manifest = {"surfaces": [{"key": "post", "content_types": ["text"], "spec": {}}]}

    # Append mode adds disclosure
    res_append = tailor_for_destination(content_ai, dest_append, manifest=manifest)
    assert res_append["ready_for_publishing"] is True
    assert "AI-generated." in res_append["tailored_content"]["body"]
    assert any(t["type"] == "ai_disclosure" for t in res_append["applied_transformations"])

    # Human-authored content does NOT get false AI disclosure
    content_human = {
        "title": "Human Authored Post",
        "body": "Written completely by a human author.",
        "ai_generated": False,
    }
    res_human = tailor_for_destination(content_human, dest_append, manifest=manifest)
    assert res_human["ready_for_publishing"] is True
    assert "AI-generated" not in res_human["tailored_content"]["body"]

    # Block mode rejects if disclosure is missing
    dest_block = {
        "id": "dest-ai-blk",
        "connector_key": "noop",
        "surface": "post",
        "disclosure_required": True,
        "disclosure_mode": "block",
    }
    res_block = tailor_for_destination(content_ai, dest_block, manifest=manifest)
    assert res_block["ready_for_publishing"] is False
    assert "This destination requires an AI-content disclosure." in res_block["validation_errors"]


# --- Test 12: Self-promotion restrictions ---
def test_self_promotion_restrictions():
    content_promo = {
        "title": "Buy Our Super Product",
        "body": "Special 50% discount this week only!",
        "is_promotional": True,
    }
    dest_no_promo = {
        "id": "dest-no-promo",
        "connector_key": "noop",
        "surface": "post",
        "self_promo_allowed": False,
    }
    manifest = {"surfaces": [{"key": "post", "content_types": ["text"], "spec": {}}]}
    result = tailor_for_destination(content_promo, dest_no_promo, manifest=manifest)
    assert result["ready_for_publishing"] is False
    assert "This destination does not allow self-promotion." in result["validation_errors"]
    assert any(r["transformation"] == "self_promotion" for r in result["rejected_transformations"])
    # Ensure creator content was not silently altered
    assert result["tailored_content"]["body"] == content_promo["body"]


# --- Test 13: Frequency-cap rules remaining enforced ---
def test_frequency_cap_rules_remaining_enforced():
    content = {"title": "Daily Post", "body": "Update for today."}
    dest = {
        "id": "dest-capped",
        "connector_key": "noop",
        "surface": "post",
        "frequency_cap": {"max_posts": 1, "window_seconds": 3600},
    }
    manifest = {"surfaces": [{"key": "post", "content_types": ["text"], "spec": {}}]}
    recent_history = [(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()]

    result = tailor_for_destination(content, dest, manifest=manifest, post_history=recent_history)
    assert result["ready_for_publishing"] is False
    assert any("Frequency cap reached" in err for err in result["validation_errors"])


# --- Test 14: Missing or contradictory manifest limits ---
def test_missing_or_contradictory_manifest_limits():
    content = {"title": "Robustness Check", "body": "Checking graceful handling."}
    dest = {"id": "dest-robust", "connector_key": "noop", "surface": "post"}

    # Contradictory negative limits in manifest
    manifest_negative = {
        "surfaces": [
            {"key": "post", "content_types": ["text"], "spec": {"title_max_chars": -10, "body_max_chars": -50}}
        ]
    }
    res_neg = tailor_for_destination(content, dest, manifest=manifest_negative)
    assert res_neg["ready_for_publishing"] is True
    assert any("Manifest title_max_chars is invalid" in w for w in res_neg["warnings"])
    assert any("Manifest body_max_chars is invalid" in w for w in res_neg["warnings"])

    # Contradictory min > max
    manifest_contradictory = {
        "surfaces": [
            {"key": "post", "content_types": ["text"], "spec": {"title_min_chars": 100, "title_max_chars": 20}}
        ]
    }
    res_contra = tailor_for_destination(content, dest, manifest=manifest_contradictory)
    assert any("contradictory title length limits" in w for w in res_contra["warnings"])


# --- Test 15: Multiple destinations receiving different tailored renditions ---
def test_multiple_destinations_receive_different_renditions(client):
    dest_devto = create_destination(client, "devto", surface="article")
    dest_discourse = create_destination(client, "discourse", surface="topic")
    dest_noop = create_destination(client, "noop", surface="post")

    content = {
        "title": "# The Future of Open Source Distribution",
        "body": "# The Future of Open Source Distribution\n\nHow federated ecosystems scale.",
        "canonical_url": "https://source.example.com/post",
        "hashtags": ["OpenSource", "Federation", "DevOps", "AI", "Cloud"],
    }

    response = client.post(
        "/api/v1/tailoring/preview",
        headers={"Idempotency-Key": "tailoring-preview-multi"},
        json={
            "content_item": content,
            "destination_ids": [dest_devto, dest_discourse, dest_noop],
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["ready_for_publishing"] is True
    dest_results = {r["destination_id"]: r for r in data["destinations"]}

    # Devto rendition:
    # - duplicate H1 removed from body
    # - tags limited to 4
    # - native canonical_url included
    devto_rendition = dest_results[dest_devto]["tailored_content"]
    assert not devto_rendition["body"].startswith("# The Future")
    assert len(devto_rendition["hashtags"]) == 4
    assert devto_rendition["canonical_url"] == "https://source.example.com/post"

    # Discourse rendition:
    # - discussion prompt appended
    # - all 5 tags retained
    discourse_rendition = dest_results[dest_discourse]["tailored_content"]
    assert "What are your thoughts on this?" in discourse_rendition["body"]
    assert len(discourse_rendition["hashtags"]) == 5

    # Noop rendition:
    # - standard post, text-only
    noop_rendition = dest_results[dest_noop]["tailored_content"]
    assert noop_rendition["content_type"] == "text"


# --- Test 16: Repeated requests producing deterministic results ---
def test_repeated_requests_produce_deterministic_results():
    content = {
        "title": "A Deterministic Title Test",
        "body": "Body content that will be processed multiple times deterministically.",
        "canonical_url": "https://example.com/canonical",
        "hashtags": ["alpha", "beta", "gamma"],
        "mentions": ["shiv", "antigravity"],
    }
    dest = {
        "id": "dest-det",
        "connector_key": "devto",
        "platform_key": "devto",
        "surface": "article",
        "link_policy": "allow",
    }
    manifest = {
        "surfaces": [
            {"key": "article", "content_types": ["text"], "spec": {"title_max_chars": 50, "body_max_chars": 200}}
        ]
    }

    run1 = tailor_for_destination(content, dest, manifest=manifest)
    run2 = tailor_for_destination(content, dest, manifest=manifest)
    run3 = tailor_for_destination(content, dest, manifest=manifest)

    assert run1 == run2 == run3
    assert run1["original_content_ref"] == run2["original_content_ref"] == run3["original_content_ref"]


# --- Test 17: Original content remaining unchanged ---
def test_original_content_remaining_unchanged():
    original = {
        "title": "Original Immutable Title",
        "body": "Original body text that must never be modified by tailoring.",
        "hashtags": ["tag1", "tag2"],
        "is_promotional": False,
    }
    snapshot = copy.deepcopy(original)
    dest = {
        "id": "dest-immut",
        "connector_key": "noop",
        "surface": "post",
        "link_policy": "strip",
    }
    manifest = {"surfaces": [{"key": "post", "content_types": ["text"], "spec": {"title_max_chars": 15}}]}

    _ = tailor_for_destination(original, dest, manifest=manifest)
    # Verify exact equality with snapshot
    assert original == snapshot


# --- Integration Test: Asynchronous background preview job & Idempotency ---
def test_async_tailoring_preview_and_idempotency(client):
    dest_id = create_destination(client, "noop")
    content = {"title": "Async Test", "body": "Testing async tailoring preview job."}

    # Request via explicit async endpoint with Idempotency-Key
    response = client.post(
        "/api/v1/tailoring-jobs",
        headers={"Idempotency-Key": "tailor-async-key-1"},
        json={"content_item": content, "destination_ids": [dest_id]},
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    job = wait_for_job(client, job_id)
    assert job["status"] == "succeeded"
    assert "tailoring_preview" in job["result"]
    preview = job["result"]["tailoring_preview"]
    assert preview["ready_for_publishing"] is True
    assert preview["destinations"][0]["destination_id"] == dest_id

    # Reusing same idempotency key returns the same job
    dup = client.post(
        "/api/v1/tailoring-jobs",
        headers={"Idempotency-Key": "tailor-async-key-1"},
        json={"content_item": content, "destination_ids": [dest_id]},
    )
    assert dup.status_code == 202
    assert dup.json()["job_id"] == job_id


# --- Integration Test: Publishing Job with enable_tailoring ---
def test_publishing_job_with_enable_tailoring(client):
    # Destination with title limit of 20 chars
    dest_id = create_destination(client, "noop")
    with client.app.state.session_factory.begin() as session:
        destination = session.get(Destination, dest_id)
        destination.level = "L3"
        destination.last_certified_at = datetime.now(timezone.utc)
    long_content = {
        "title": "A title that is definitely way too long for destination limits and goes on and on and on to exceed one hundred and twenty characters easily without stopping.",
        "body": "A valid body that fits within limits.",
    }

    # Submit publishing job with enable_tailoring=True
    response = client.post(
        "/api/v1/publishing-jobs",
        headers={"Idempotency-Key": "publish-with-tailor-1"},
        json={
            "content_item": long_content,
            "destination_ids": [dest_id],
            "enable_tailoring": True,
        },
    )
    assert response.status_code == 202
    job = wait_for_job(client, response.json()["job_id"])
    assert job["status"] == "succeeded"
    outcome = job["result"]["destinations"][0]
    assert outcome["status"] == "published"
    assert "tailored_content" in outcome
    assert len(outcome["tailored_content"]["title"]) <= 120
    assert any(t["type"] == "title_truncation" for t in outcome["applied_transformations"])
