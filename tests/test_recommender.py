"""Tests for the explainable destination recommendation engine."""

from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
import pytest

from app.database import Destination, Job
from app.evaluation import run_recommender_evaluation
from app.recommender import (
    DEFAULT_WEIGHTS,
    RecommenderWeights,
    RecommendationService,
    score_destination,
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


# --- Test 1: Exact topic and audience matches ranking highly ---
def test_exact_topic_and_audience_matches_rank_highly():
    content = {
        "title": "Machine Learning and LLM Architectures",
        "body": "Detailed discussion of AI, neural networks, and prompt engineering.",
        "content_type": "text",
        "hashtags": ["AI", "MachineLearning"],
    }
    ai_dest = {
        "id": "dest-ai",
        "name": "AI Engineers Community",
        "category": "AI",
        "audience_tags": ["AI", "Machine Learning"],
        "relevance_score": 5,
        "level": "L3",
        "connector_key": "noop",
        "surface": "post",
    }
    gardening_dest = {
        "id": "dest-garden",
        "name": "Organic Gardening Club",
        "category": "Gardening",
        "audience_tags": ["Plants", "Vegetables"],
        "relevance_score": 1,
        "level": "L0",
        "connector_key": "noop",
        "surface": "post",
    }
    scored_ai = score_destination(content, ai_dest, topic="AI", audience_tags=["AI"])
    scored_garden = score_destination(content, gardening_dest, topic="AI", audience_tags=["AI"])

    assert scored_ai["score"] > scored_garden["score"]
    assert scored_ai["score"] >= 75.0
    assert "AI" in scored_ai["matched_categories"]
    assert any("Matched audience categories" in sig for sig in scored_ai["positive_signals"])


# --- Test 2: Weakly relevant destinations ranking lower ---
def test_weakly_relevant_destinations_rank_lower():
    content = {
        "title": "B2B SaaS Growth Hacks",
        "body": "Scaling subscription recurring revenue.",
        "hashtags": ["SaaS", "Growth"],
    }
    strong_dest = {
        "id": "dest-saas",
        "name": "SaaS Leaders Forum",
        "category": "SaaS",
        "audience_tags": ["SaaS", "Founders"],
        "relevance_score": 5,
        "level": "L3",
    }
    weak_dest = {
        "id": "dest-general",
        "name": "General Tech Chat",
        "category": "General",
        "audience_tags": ["Discussion"],
        "relevance_score": 2,
        "level": "L1",
    }
    scored_strong = score_destination(content, strong_dest, topic="SaaS")
    scored_weak = score_destination(content, weak_dest, topic="SaaS")

    assert scored_strong["score"] > scored_weak["score"]
    assert any("Weak topic/audience alignment" in sig for sig in scored_weak["negative_signals"])


# --- Test 3: Language compatibility ---
def test_language_compatibility():
    content_es = {
        "title": "Tutorial en Español",
        "body": "Aprende programación en Python.",
        "language": "es",
    }
    dest_es = {"id": "d-es", "name": "Comunidad Hispana", "language": "es"}
    dest_de = {"id": "d-de", "name": "Deutsche Entwickler", "language": "de"}
    dest_any = {"id": "d-any", "name": "Global Forum", "language": None}

    score_es = score_destination(content_es, dest_es)
    score_de = score_destination(content_es, dest_de)
    score_any = score_destination(content_es, dest_any)

    assert score_es["score"] > score_de["score"]
    assert score_any["score"] > score_de["score"]
    assert any("Content language 'es' differs from destination expected 'de'" in sig for sig in score_de["negative_signals"])
    assert any("Language compatible" in sig for sig in score_es["positive_signals"])


# --- Test 4: Content-type incompatibility ---
def test_content_type_incompatibility():
    video_content = {
        "title": "New Video Guide",
        "body": "Watch the presentation.",
        "content_type": "video_link",
    }
    text_only_dest = {"id": "d-text", "name": "Text Forum", "surface": "post"}
    manifest = {
        "surfaces": [
            {"key": "post", "content_types": ["text"], "spec": {}}
        ]
    }
    result = score_destination(video_content, text_only_dest, connector_manifest=manifest)
    assert result["eligibility"] == "excluded"
    assert any("not supported by surface" in excl for excl in result["exclusions"])


# --- Test 5: Blocked self-promotion ---
def test_blocked_self_promotion():
    promo_content = {
        "title": "Special Deal",
        "body": "Get 50% off right now!",
        "is_promotional": True,
    }
    dest_no_promo = {
        "id": "d-nopromo",
        "name": "Strict Rules Community",
        "self_promo_allowed": False,
        "category": "AI",
        "relevance_score": 5,
    }
    result = score_destination(promo_content, dest_no_promo, topic="AI")
    # Even if highly relevant, rule violation flags it as blocked
    assert result["eligibility"] == "recommended_rule_blocked"
    assert "This destination does not allow self-promotion." in result["exclusions"]
    assert result["rule_eligibility"]["outcome"] == "blocked"


# --- Test 6: Link restrictions ---
def test_link_restrictions():
    content_with_links = {
        "title": "Read More",
        "body": "Visit https://example.test for details.",
    }
    dest_block_links = {
        "id": "d-nolinks",
        "name": "No Links Group",
        "link_policy": "block",
    }
    result = score_destination(content_with_links, dest_block_links)
    assert result["eligibility"] == "recommended_rule_blocked"
    assert "This destination does not allow links." in result["exclusions"]


# --- Test 7: Disclosure requirements ---
def test_disclosure_requirements():
    ai_content = {
        "title": "Generated Summary",
        "body": "Pure AI summary without notice.",
        "ai_generated": True,
    }
    dest_require_disclosure = {
        "id": "d-disclosure",
        "name": "Human-Only Review",
        "disclosure_required": True,
        "disclosure_mode": "block",
    }
    result = score_destination(ai_content, dest_require_disclosure)
    assert result["eligibility"] == "recommended_rule_blocked"
    assert "This destination requires an AI-content disclosure." in result["exclusions"]


# --- Test 8: Frequency caps ---
def test_frequency_caps():
    content = {"title": "Update", "body": "Standard post."}
    dest_capped = {
        "id": "d-cap",
        "name": "Rate Limited Community",
        "frequency_cap": {"max_posts": 1, "window_seconds": 3600},
    }
    recent_history = [(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()]
    result = score_destination(content, dest_capped, post_history=recent_history)
    assert result["eligibility"] == "recommended_rule_blocked"
    assert "Frequency cap reached for this destination." in result["exclusions"]


# --- Test 9: Assisted-only destinations ---
def test_assisted_only_destinations():
    content = {"title": "Article", "body": "Good content."}
    assisted_dest = {
        "id": "d-assist",
        "name": "Substack Manual Entry",
        "connector_key": "assisted",
        "automation_allowed": False,
        "self_promo_allowed": True,
    }
    result = score_destination(content, assisted_dest)
    assert result["eligibility"] == "recommended_assisted_only"
    assert "requires assisted manual posting" in result["explanation"]


# --- Test 10: Uncertified or unavailable connectors ---
def test_uncertified_or_unavailable_connectors():
    content = {"title": "Dev Post", "body": "Content body."}
    disabled_dest = {
        "id": "d-disabled",
        "name": "Disabled Forum",
        "connector_key": "webhook",
    }
    result = score_destination(content, disabled_dest, connector_disabled=True)
    assert result["eligibility"] == "excluded"
    assert any("disabled by configuration" in excl for excl in result["exclusions"])


# --- Test 11: Missing historical metrics ---
def test_missing_historical_metrics():
    content = {"title": "First Time Post", "body": "No historical metrics exist."}
    dest = {"id": "d-new", "name": "Brand New Destination"}
    result = score_destination(content, dest, historical_outcomes=None)

    assert any("Historical publication data unavailable" in sig for sig in result["positive_signals"])
    assert any("Historical performance metrics unavailable; engagement personalization not active." in w for w in result["warnings"])


# --- Test 12: Unknown publishing outcomes not counted as successes ---
def test_unknown_publishing_outcomes_not_counted_as_successes():
    content = {"title": "Safety Test", "body": "Verifying outcome_unknown handling."}
    dest = {"id": "d-uncertain", "name": "Unstable Platform"}

    # 1 published, 3 outcome_unknown, 0 failed -> success rate should be 1/4 (25%), NOT 4/4 (100%)
    outcomes_with_unknown = {
        "published": 1,
        "assisted": 0,
        "failed": 0,
        "outcome_unknown": 3,
        "total": 4,
    }
    # 4 confirmed published
    outcomes_confirmed = {
        "published": 4,
        "assisted": 0,
        "failed": 0,
        "outcome_unknown": 0,
        "total": 4,
    }

    res_uncertain = score_destination(content, dest, historical_outcomes=outcomes_with_unknown)
    res_confirmed = score_destination(content, dest, historical_outcomes=outcomes_confirmed)

    assert res_confirmed["score"] > res_uncertain["score"]
    assert any("Historical outcomes include 3 uncertain publication(s) not counted as successes." in w for w in res_uncertain["warnings"])


# --- Test 13: Stable ordering when scores tie ---
def test_stable_ordering_when_scores_tie(client):
    dest_a = create_destination(client, "noop", name="Alpha Destination")
    dest_b = create_destination(client, "noop", name="Beta Destination")
    dest_c = create_destination(client, "noop", name="Gamma Destination")

    content = {"title": "Tie Breaker", "body": "Testing stable ordering."}
    response1 = client.post("/api/v1/recommendations", headers={"Idempotency-Key": "recommend-1"}, json={"content_item": content})
    response2 = client.post("/api/v1/recommendations", headers={"Idempotency-Key": "recommend-2"}, json={"content_item": content})

    data1 = response1.json()
    data2 = response2.json()

    # Ranks and order must match identically across repeated requests
    names1 = [r["destination_name"] for r in data1["recommendations"]]
    names2 = [r["destination_name"] for r in data2["recommendations"]]
    assert names1 == names2


# --- Test 14: Minimum-score and top-K filtering ---
def test_minimum_score_and_top_k_filtering(client):
    dest_ids = [
        create_destination(client, "noop", name=f"Dest {i}", relevance_score=min(5, (i % 5) + 1))
        for i in range(12)
    ]
    content = {"title": "Filtering Test", "body": "Checking top_k and min_score."}

    # Top-K = 3
    res_k = client.post("/api/v1/recommendations", headers={"Idempotency-Key": "recommend-topk"}, json={"content_item": content, "top_k": 3})
    assert len(res_k.json()["recommendations"]) == 3

    # High min_score threshold
    res_min = client.post("/api/v1/recommendations", headers={"Idempotency-Key": "recommend-min"}, json={"content_item": content, "min_score": 90.0})
    for r in res_min.json()["recommendations"]:
        assert r["score"] >= 90.0


# --- Test 15: Explanation consistency with actual scoring signals ---
def test_explanation_consistency_with_actual_scoring_signals():
    content = {
        "title": "Enterprise Cloud Architecture",
        "body": "SaaS and AI backend patterns.",
    }
    dest = {
        "id": "d-explain",
        "name": "Cloud Hub",
        "category": "SaaS",
        "relevance_score": 5,
        "level": "L3",
        "self_promo_allowed": True,
        "automation_allowed": True,
    }
    result = score_destination(content, dest, topic="SaaS")
    assert "SaaS" in result["explanation"]
    assert "5/5 relevance" in result["explanation"]
    assert "ready for immediate publishing" in result["explanation"]


# --- Test 16: Empty catalogue and no eligible destinations ---
def test_empty_catalogue(app):
    service = RecommendationService(app.state.session_factory, app.state.connector_registry)
    result = service.recommend({"title": "Test", "body": "Test"}, candidate_destination_ids=["non-existent-id"])
    assert result["total_candidates"] == 0
    assert result["recommendations"] == []
    assert result["exclusions"] == []


# --- Test 17: Multiple content types and audience categories ---
def test_multiple_content_types_and_audience_categories(client):
    create_destination(client, "noop", name="General Dev Hub", category="Software", audience_tags=["Developers", "AI"])
    article_content = {
        "title": "Comprehensive Guide to AI Engineering",
        "body": "Detailed deep-dive tutorial.",
        "content_type": "article",
        "hashtags": ["AI", "SaaS", "Founders"],
    }
    response = client.post(
        "/api/v1/recommendations",
        headers={"Idempotency-Key": "recommend-multi-content"},
        json={
            "content_item": article_content,
            "topic": "AI",
            "audience_tags": ["AI", "SaaS"],
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total_evaluated"] >= 1
    assert all("score" in r and "explanation" in r for r in data["recommendations"])


# --- Test 18: Reproducible offline evaluation ---
def test_reproducible_offline_evaluation():
    report = run_recommender_evaluation()
    metrics = report["metrics"]

    assert metrics["mean_recall_at_10"] >= 0.8
    assert metrics["rule_compliance_rate"] == 1.0
    assert metrics["ranking_margin"] > 15.0
    assert report["dataset_metadata"]["is_synthetic"] is True
