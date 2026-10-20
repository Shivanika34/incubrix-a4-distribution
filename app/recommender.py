"""Explainable destination recommendation and ranking engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import re
from typing import Any

from sqlalchemy import select

from app.catalogue import TARGETS, score_relevance
from app.database import Destination, Job
from app.rules import check_rules

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecommenderWeights:
    """Configurable scoring weights summing to 100 points."""

    audience_topic_relevance: float = 30.0
    catalogue_relevance: float = 15.0
    language_compatibility: float = 10.0
    content_surface_compatibility: float = 15.0
    creator_preferences: float = 10.0
    certification_readiness: float = 10.0
    historical_success: float = 10.0


DEFAULT_WEIGHTS = RecommenderWeights()

LEVEL_VALUES = {
    "L4": 1.0,
    "L3": 0.9,
    "L2": 0.7,
    "L1": 0.5,
    "L0": 0.25,
}


class AnalyticsProvider:
    """Interface for future engagement/conversion analytics."""

    def get_destination_engagement(self, destination_id: str) -> dict[str, Any] | None:
        """Return measured engagement or conversion data if available; None indicates no data."""
        return None


def extract_historical_outcomes(
    session, destination_id: str
) -> dict[str, int]:
    """Inspect stored publishing jobs to extract trustworthy historical outcomes.

    Never counts outcome_unknown as a success.
    """
    query = select(Job).where(Job.job_type == "publishing")
    jobs = session.scalars(query).all()
    published = 0
    assisted = 0
    failed = 0
    unknown = 0

    for job in jobs:
        destinations = (job.result or {}).get("destinations", [])
        for item in destinations:
            if item.get("destination_id") == destination_id:
                status = item.get("status")
                if status == "published":
                    published += 1
                elif status == "assisted":
                    assisted += 1
                elif status == "outcome_unknown":
                    unknown += 1
                elif status == "failed":
                    failed += 1

    return {
        "published": published,
        "assisted": assisted,
        "failed": failed,
        "outcome_unknown": unknown,
        "total": published + assisted + failed + unknown,
    }


def evaluate_eligibility(
    content_item: dict[str, Any],
    destination: dict[str, Any],
    surface_spec: dict[str, Any],
    allowed_content_types: list[str],
    connector_disabled: bool,
    post_history: list[Any] | None = None,
) -> tuple[str, dict[str, Any], list[str]]:
    """Determine policy and connector readiness.

    Returns (eligibility, rule_details, exclusion_reasons).
    Possible eligibility values:
    - 'excluded'
    - 'recommended_rule_blocked'
    - 'recommended_assisted_only'
    - 'recommended_and_ready'
    """
    exclusions: list[str] = []
    content_type = str(content_item.get("content_type") or "text")

    # Incompatibility exclusions
    if connector_disabled:
        exclusions.append(f"Connector '{destination.get('connector_key')}' is disabled by configuration.")
        return "excluded", {"status": "connector_disabled"}, exclusions

    if content_type not in allowed_content_types:
        if not (content_type == "article" and "text" in allowed_content_types):
            exclusions.append(
                f"Content type '{content_type}' is not supported by surface '{destination.get('surface')}' "
                f"(supported: {', '.join(allowed_content_types)})."
            )
            return "excluded", {"status": "unsupported_content_type"}, exclusions

    # Run posting-rule checks
    rule_decision = check_rules(content_item, destination, post_history or [])
    rule_details = {
        "outcome": rule_decision.outcome,
        "reason": rule_decision.reason,
        "error_code": rule_decision.error_code,
    }

    assisted_reason = "This destination requires assisted posting."
    is_assisted = (
        not destination.get("automation_allowed", True)
        or destination.get("connector_key") == "assisted"
    )

    if rule_decision.outcome == "blocked":
        if rule_decision.reason == assisted_reason or is_assisted:
            return "recommended_assisted_only", rule_details, []
        exclusions.append(rule_decision.reason)
        return "recommended_rule_blocked", rule_details, exclusions

    if rule_decision.outcome == "reschedule":
        exclusions.append(rule_decision.reason)
        return "recommended_rule_blocked", rule_details, exclusions

    if is_assisted:
        return "recommended_assisted_only", rule_details, []

    return "recommended_and_ready", rule_details, []


def score_destination(
    content_item: dict[str, Any],
    destination: dict[str, Any],
    *,
    topic: str | None = None,
    audience_tags: list[str] | None = None,
    creator_preferences: dict[str, Any] | None = None,
    connector_manifest: dict[str, Any] | None = None,
    connector_disabled: bool = False,
    historical_outcomes: dict[str, int] | None = None,
    weights: RecommenderWeights = DEFAULT_WEIGHTS,
    post_history: list[Any] | None = None,
) -> dict[str, Any]:
    """Compute normalized 0-100 explainable recommendation score and eligibility."""
    creator_preferences = creator_preferences or {}
    audience_tags = [t.strip().lower() for t in (audience_tags or []) if t.strip()]
    positive_signals: list[str] = []
    negative_signals: list[str] = []
    warnings: list[str] = []

    dest_id = str(destination.get("id") or "")
    dest_name = str(destination.get("name") or "Unnamed Destination")
    surface_key = str(destination.get("surface") or "post")

    # Locate manifest surface
    surface_spec: dict[str, Any] = {}
    allowed_content_types: list[str] = ["text"]
    if connector_manifest and connector_manifest.get("surfaces"):
        matched = next((s for s in connector_manifest["surfaces"] if s.get("key") == surface_key), None)
        if matched is None:
            matched = connector_manifest["surfaces"][0]
        surface_spec = matched.get("spec", {})
        allowed_content_types = matched.get("content_types", ["text"])

    # --- 1. Audience & Topic Relevance (0 to 1.0 * weight) ---
    content_text = " ".join([
        str(content_item.get("title") or ""),
        str(content_item.get("body") or ""),
        str(topic or ""),
    ]).casefold()

    dest_tags = [str(t).strip().lower() for t in (destination.get("audience_tags") or [])]
    dest_text = " ".join([
        str(destination.get("name") or ""),
        str(destination.get("category") or ""),
        str(destination.get("description") or ""),
        str(destination.get("subject_area") or ""),
        str(destination.get("relevance_reason") or ""),
    ]).casefold()

    matched_categories: list[str] = []
    matched_tags: list[str] = []

    # Category matching via TARGETS
    for category, terms in TARGETS.items():
        content_has_cat = any(re.search(rf"(?<!\w){re.escape(term)}(?!\w)", content_text) for term in terms)
        dest_has_cat = any(re.search(rf"(?<!\w){re.escape(term)}(?!\w)", dest_text + " " + " ".join(dest_tags)) for term in terms)
        if content_has_cat and dest_has_cat:
            matched_categories.append(category)

    # Tag matching
    combined_content_tags = set(audience_tags) | {
        str(t).strip().lower().lstrip("#")
        for t in (content_item.get("hashtags") or [])
    }
    for tag in combined_content_tags:
        if tag in dest_tags or tag in dest_text:
            matched_tags.append(tag)

    if matched_categories or matched_tags:
        cat_ratio = 1.0 if len(matched_categories) >= 1 else 0.0
        tag_ratio = min(1.0, len(matched_tags) / 1.0) if matched_tags else 0.0
        if cat_ratio and tag_ratio:
            audience_ratio = 1.0
        else:
            audience_ratio = max(cat_ratio, tag_ratio)
        audience_pts = round(audience_ratio * weights.audience_topic_relevance, 2)
        positive_signals.append(
            f"+{audience_pts:.1f} pts: Matched audience categories ({', '.join(matched_categories) or 'none'}) "
            f"and tags ({', '.join(matched_tags) or 'none'})."
        )
    else:
        audience_ratio = 0.1  # Minimal baseline for general discovery
        audience_pts = round(audience_ratio * weights.audience_topic_relevance, 2)
        negative_signals.append(f"-{weights.audience_topic_relevance - audience_pts:.1f} pts: Weak topic/audience alignment.")

    # --- 2. Catalogue Relevance Score (0 to 1.0 * weight) ---
    cat_score = destination.get("relevance_score")
    if cat_score is None:
        scored = score_relevance(destination)
        cat_score = scored.get("relevance_score", 1)

    cat_ratio = max(0.0, min(1.0, (int(cat_score) - 1) / 4.0))
    cat_pts = round(cat_ratio * weights.catalogue_relevance, 2)
    if cat_score >= 3:
        positive_signals.append(f"+{cat_pts:.1f} pts: Catalogue relevance score is {cat_score}/5.")
    else:
        negative_signals.append(f"-{weights.catalogue_relevance - cat_pts:.1f} pts: Lower catalogue baseline relevance ({cat_score}/5).")

    # --- 3. Language Compatibility (0 or 1.0 * weight) ---
    content_lang = content_item.get("language")
    dest_lang = destination.get("language")
    if not dest_lang or not content_lang or str(dest_lang).lower() == str(content_lang).lower():
        lang_ratio = 1.0
        lang_pts = weights.language_compatibility
        positive_signals.append(f"+{lang_pts:.1f} pts: Language compatible ({content_lang or 'unspecified'}).")
    else:
        lang_ratio = 0.0
        lang_pts = 0.0
        negative_signals.append(
            f"-{weights.language_compatibility:.1f} pts: Content language '{content_lang}' differs from destination expected '{dest_lang}'."
        )
        warnings.append(f"Content language '{content_lang}' does not match destination expected language '{dest_lang}'.")

    # --- 4. Content-Type and Surface Compatibility (0 to 1.0 * weight) ---
    req_type = str(content_item.get("content_type") or "text")
    if req_type in allowed_content_types or (req_type == "article" and "text" in allowed_content_types):
        surface_ratio = 1.0
        surface_pts = weights.content_surface_compatibility
        positive_signals.append(f"+{surface_pts:.1f} pts: Surface '{surface_key}' natively supports content type '{req_type}'.")
    else:
        surface_ratio = 0.0
        surface_pts = 0.0
        negative_signals.append(
            f"-{weights.content_surface_compatibility:.1f} pts: Surface '{surface_key}' does not support content type '{req_type}'."
        )

    # --- 5. Creator Preferences (0 to 1.0 * weight) ---
    pref_score = 0.5  # Neutral default
    pref_reasons: list[str] = []

    preferred_platforms = [p.lower() for p in creator_preferences.get("preferred_platforms", [])]
    if preferred_platforms:
        platform = str(destination.get("platform_key") or destination.get("connector_key") or "").lower()
        if platform in preferred_platforms:
            pref_score += 0.3
            pref_reasons.append(f"Platform '{platform}' matches creator preferences.")
        else:
            pref_score -= 0.2

    preferred_level = creator_preferences.get("preferred_level")
    if preferred_level:
        dest_level = str(destination.get("level") or "L0")
        if dest_level >= str(preferred_level):
            pref_score += 0.2
            pref_reasons.append(f"Certification level '{dest_level}' satisfies preferred '{preferred_level}'.")
        else:
            pref_score -= 0.2

    pref_ratio = max(0.0, min(1.0, pref_score))
    pref_pts = round(pref_ratio * weights.creator_preferences, 2)
    if pref_reasons:
        positive_signals.append(f"+{pref_pts:.1f} pts: Creator preference alignment ({'; '.join(pref_reasons)}).")
    elif creator_preferences:
        negative_signals.append(f"-{weights.creator_preferences - pref_pts:.1f} pts: Partial creator preference alignment.")

    # --- 6. Certification Readiness (0 to 1.0 * weight) ---
    dest_level = str(destination.get("level") or "L0")
    level_ratio = LEVEL_VALUES.get(dest_level, 0.25)
    if connector_disabled:
        cert_ratio = 0.0
        negative_signals.append(f"-{weights.certification_readiness:.1f} pts: Connector is disabled by configuration.")
    else:
        cert_ratio = level_ratio
        cert_pts = round(cert_ratio * weights.certification_readiness, 2)
        if dest_level in {"L3", "L4"}:
            positive_signals.append(f"+{cert_pts:.1f} pts: Verified certification level {dest_level}.")
        else:
            positive_signals.append(f"+{cert_pts:.1f} pts: Unverified or assisted certification level ({dest_level}).")

    # --- 7. Historical Publishing Success (0 to 1.0 * weight) ---
    if historical_outcomes and historical_outcomes.get("total", 0) > 0:
        pub_count = historical_outcomes.get("published", 0)
        # Note: outcome_unknown is counted in attempts, never in successful publications!
        total_attempts = historical_outcomes["total"]
        hist_ratio = pub_count / total_attempts if total_attempts > 0 else 0.5
        hist_pts = round(hist_ratio * weights.historical_success, 2)
        if hist_ratio >= 0.7:
            positive_signals.append(
                f"+{hist_pts:.1f} pts: High historical success rate ({pub_count}/{total_attempts} confirmed published)."
            )
        else:
            negative_signals.append(
                f"-{weights.historical_success - hist_pts:.1f} pts: Historical success rate is {hist_ratio:.0%} "
                f"({pub_count}/{total_attempts} confirmed published)."
            )
        if historical_outcomes.get("outcome_unknown", 0) > 0:
            warnings.append(
                f"Historical outcomes include {historical_outcomes['outcome_unknown']} uncertain publication(s) "
                "not counted as successes."
            )
    else:
        # Trustworthy metrics unavailable: apply neutral default without fabricating metrics
        hist_ratio = 0.5
        hist_pts = round(hist_ratio * weights.historical_success, 2)
        positive_signals.append(
            f"+{hist_pts:.1f} pts: Historical publication data unavailable (neutral default applied)."
        )
        warnings.append("Historical performance metrics unavailable; engagement personalization not active.")

    # --- Aggregate Normalized Score (0–100) ---
    total_score = round(
        audience_pts
        + cat_pts
        + lang_pts
        + surface_pts
        + pref_pts
        + round(cert_ratio * weights.certification_readiness, 2)
        + hist_pts,
        1,
    )
    total_score = max(0.0, min(100.0, total_score))

    # --- Evaluate Eligibility & Restrictions ---
    eligibility, rule_details, rule_exclusions = evaluate_eligibility(
        content_item,
        destination,
        surface_spec,
        allowed_content_types,
        connector_disabled,
        post_history,
    )
    if rule_exclusions:
        warnings.extend(rule_exclusions)
        for excl in rule_exclusions:
            negative_signals.append(f"Policy / readiness restriction: {excl}")

    # Generate evidence-based explanation
    explanation_parts: list[str] = []
    if matched_categories:
        explanation_parts.append(f"Strong topic match in {', '.join(matched_categories)}")
    elif matched_tags:
        explanation_parts.append(f"Matches audience tags: {', '.join(matched_tags)}")
    else:
        explanation_parts.append("General audience fit based on catalogue taxonomy")

    explanation_parts.append(f"scored {cat_score}/5 relevance")
    explanation_parts.append(f"certification level {dest_level}")
    if eligibility == "recommended_and_ready":
        explanation_parts.append("ready for immediate publishing")
    elif eligibility == "recommended_assisted_only":
        explanation_parts.append("requires assisted manual posting")
    elif eligibility == "recommended_rule_blocked":
        explanation_parts.append("currently blocked by posting rules")
    elif eligibility == "excluded":
        explanation_parts.append("incompatible with content or connector specification")

    concise_explanation = "; ".join(explanation_parts) + "."

    return {
        "destination_id": dest_id,
        "destination_name": dest_name,
        "score": total_score,
        "matched_categories": matched_categories,
        "matched_tags": matched_tags,
        "explanation": concise_explanation,
        "positive_signals": positive_signals,
        "negative_signals": negative_signals,
        "eligibility": eligibility,
        "rule_eligibility": rule_details,
        "readiness": {
            "level": dest_level,
            "connector_key": destination.get("connector_key"),
            "connector_disabled": connector_disabled,
            "last_certified_at": destination.get("last_certified_at"),
        },
        "warnings": warnings,
        "exclusions": rule_exclusions,
    }


class RecommendationService:
    """Service coordinates destination ranking, eligibility checks, and filtering."""

    def __init__(
        self,
        session_factory,
        connector_registry=None,
        disabled_connectors: frozenset[str] = frozenset(),
        weights: RecommenderWeights = DEFAULT_WEIGHTS,
        analytics_provider: AnalyticsProvider | None = None,
    ):
        self.session_factory = session_factory
        self.connector_registry = connector_registry
        self.disabled_connectors = disabled_connectors
        self.weights = weights
        self.analytics_provider = analytics_provider or AnalyticsProvider()

    def recommend(
        self,
        content_item: dict[str, Any],
        *,
        topic: str | None = None,
        audience_tags: list[str] | None = None,
        candidate_destination_ids: list[str] | None = None,
        creator_preferences: dict[str, Any] | None = None,
        top_k: int = 10,
        min_score: float = 0.0,
        include_excluded: bool = False,
    ) -> dict[str, Any]:
        """Rank and return eligible destinations with explanations."""
        with self.session_factory() as session:
            query = select(Destination)
            if candidate_destination_ids:
                query = query.where(Destination.id.in_(candidate_destination_ids))
            destinations = session.scalars(query).all()
            dest_dicts = [
                {col.name: getattr(d, col.name) for col in Destination.__table__.columns}
                for d in destinations
            ]

            scored_items: list[dict[str, Any]] = []
            for dest in dest_dicts:
                dest_id = dest["id"]
                conn_key = dest.get("connector_key", "")
                manifest = (
                    self.connector_registry.manifests.get(conn_key)
                    if self.connector_registry
                    else None
                )
                disabled = conn_key in self.disabled_connectors
                historical = extract_historical_outcomes(session, dest_id)

                scored = score_destination(
                    content_item,
                    dest,
                    topic=topic,
                    audience_tags=audience_tags,
                    creator_preferences=creator_preferences,
                    connector_manifest=manifest,
                    connector_disabled=disabled,
                    historical_outcomes=historical,
                    weights=self.weights,
                )
                scored_items.append(scored)

        # Deterministic sorting: highest score, highest catalogue relevance, highest level, name, id
        def sort_key(item: dict[str, Any]):
            score = item["score"]
            level = item["readiness"]["level"]
            level_order = LEVEL_VALUES.get(level, 0.0)
            name = item["destination_name"].lower()
            return (-score, -level_order, name, item["destination_id"])

        scored_items.sort(key=sort_key)

        recommendations: list[dict[str, Any]] = []
        exclusions: list[dict[str, Any]] = []

        rank = 1
        for item in scored_items:
            if item["eligibility"] == "excluded":
                exclusions.append({
                    "destination_id": item["destination_id"],
                    "destination_name": item["destination_name"],
                    "score": item["score"],
                    "reason": "; ".join(item["exclusions"]) or "Excluded by specification.",
                    "warnings": item["warnings"],
                })
                if not include_excluded:
                    continue

            if item["score"] < min_score:
                exclusions.append({
                    "destination_id": item["destination_id"],
                    "destination_name": item["destination_name"],
                    "score": item["score"],
                    "reason": f"Score {item['score']} is below minimum threshold of {min_score}.",
                    "warnings": item["warnings"],
                })
                continue

            item["rank"] = rank
            rank += 1
            recommendations.append(item)
            if len(recommendations) >= top_k:
                break

        return {
            "total_candidates": len(dest_dicts),
            "total_evaluated": len(scored_items),
            "recommendations": recommendations,
            "exclusions": exclusions,
        }
