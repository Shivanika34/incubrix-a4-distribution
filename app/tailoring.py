"""Reusable content-tailoring engine for adapting content to destination platforms."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
from typing import Any

from app.database import Destination
from app.errors import AppError
from app.rules import DISCLOSURE, LINK_RE, check_rules

logger = logging.getLogger(__name__)


def compute_content_ref(content_item: dict[str, Any]) -> str:
    """Generate a deterministic, traceable reference for the source content item."""
    encoded = json.dumps(content_item, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    if content_item.get("id"):
        return f"{content_item['id']}#sha256:{digest[:16]}"
    return f"sha256:{digest[:16]}"


def truncate_at_word_boundary(
    text: str,
    max_chars: int | None,
    ellipsis: str = "...",
) -> tuple[str, bool, str | None]:
    """Truncate text at word boundaries without splitting Unicode characters.

    Returns (truncated_text, was_truncated, warning_message).
    """
    if not text:
        return text, False, None
    if max_chars is None or len(text) <= max_chars:
        return text, False, None
    if max_chars <= 0:
        return "", True, "Length limit must be positive"
    if max_chars < len(ellipsis):
        return text[:max_chars], True, f"Length limit {max_chars} is smaller than ellipsis; hard-truncated"

    target_len = max_chars - len(ellipsis)
    candidate = text[:target_len]

    # If the boundary character right after candidate is whitespace or punctuation
    if len(text) > target_len and text[target_len].isspace():
        return f"{candidate.rstrip()}{ellipsis}", True, None

    # Search for whitespace or CJK boundary backwards in candidate
    # \s matches all Unicode whitespaces
    # CJK punctuation: 、 。 ， ！ ？
    boundary_match = re.search(r'[\s\u3001\u3002\uff0c\uff01\uff1f]+(?=[^\s\u3001\u3002\uff0c\uff01\uff1f]*$)', candidate)
    if boundary_match:
        cutoff = boundary_match.start()
        trimmed = candidate[:cutoff].rstrip()
        if trimmed:
            return f"{trimmed}{ellipsis}", True, None

    # Check for CJK characters where whitespace word segmentation is not used
    has_cjk = bool(re.search(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]', candidate))
    if has_cjk and target_len > 0:
        return f"{candidate}{ellipsis}", True, None

    # Fallback when single word exceeds available limit
    return f"{candidate}{ellipsis}", True, "No word boundary found within limit; word was split"


def format_hashtags(hashtags: list[str], platform_key: str) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    """Format creator-provided hashtags for the target platform.

    Returns (formatted_hashtags, warnings, applied_transformations).
    """
    if not hashtags:
        return [], [], []

    warnings: list[str] = []
    applied: list[dict[str, Any]] = []

    if platform_key == "devto":
        # Dev.to expects lowercase alphanumeric tags without '#', max 4 tags
        cleaned: list[str] = []
        for tag in hashtags:
            norm = re.sub(r'[^a-zA-Z0-9]', '', str(tag)).lower().strip()
            if norm and norm not in cleaned:
                cleaned.append(norm)
        if len(cleaned) > 4:
            omitted = len(cleaned) - 4
            warnings.append(f"Dev.to supports at most 4 tags; omitted {omitted} tag(s).")
            cleaned = cleaned[:4]
        applied.append({"type": "hashtag_formatting", "description": f"Normalized {len(cleaned)} tags for Dev.to (lowercase, max 4)."})
        return cleaned, warnings, applied

    cleaned = []
    for tag in hashtags:
        t = str(tag).strip().lstrip("#").strip()
        t = re.sub(r'\s+', '_', t)
        if t and f"#{t}" not in cleaned:
            cleaned.append(f"#{t}")
    if cleaned:
        applied.append({"type": "hashtag_formatting", "description": f"Standardized {len(cleaned)} hashtags with '#' prefix."})
    return cleaned, warnings, applied


def format_mentions(mentions: list[str], platform_key: str) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    """Format creator-provided mentions for the target platform.

    Returns (formatted_mentions, warnings, applied_transformations).
    """
    if not mentions:
        return [], [], []

    warnings: list[str] = []
    applied: list[dict[str, Any]] = []
    cleaned: list[str] = []
    for m in mentions:
        user = str(m).strip().lstrip("@").strip()
        if user and f"@{user}" not in cleaned:
            cleaned.append(f"@{user}")
    if cleaned:
        applied.append({"type": "mention_formatting", "description": f"Standardized {len(cleaned)} mentions with '@' prefix."})
    return cleaned, warnings, applied


def apply_article_formatting(title: str, body: str) -> tuple[str, list[dict[str, Any]]]:
    """Apply article-style formatting for publishing platforms (e.g. devto, ghost)."""
    applied: list[dict[str, Any]] = []
    cleaned_body = body.strip()

    # Strip duplicate H1 markdown header that repeats the title
    if title:
        clean_title = re.sub(r'^#+\s*', '', title).strip()
        pattern = rf'^\s*#+\s+{re.escape(clean_title)}(?:\r?\n+|$)'
        match = re.match(pattern, cleaned_body, flags=re.IGNORECASE)
        if match:
            cleaned_body = cleaned_body[match.end():].lstrip()
            applied.append({
                "type": "article_formatting",
                "description": "Removed duplicate H1 title heading from article body."
            })

    # Normalize excessive blank lines
    normalized_body = re.sub(r'\n{3,}', '\n\n', cleaned_body)
    if normalized_body != cleaned_body:
        cleaned_body = normalized_body
        applied.append({
            "type": "article_formatting",
            "description": "Normalized paragraph spacing."
        })
    return cleaned_body, applied


def apply_discussion_framing(title: str, body: str) -> tuple[str, list[dict[str, Any]]]:
    """Apply discussion-first framing for forum destinations (e.g. discourse, lemmy)."""
    applied: list[dict[str, Any]] = []
    has_question = "?" in title or "?" in body
    discussion_keywords = {"what do you think", "thoughts?", "feedback welcome", "discussion", "share your experience", "what are your"}
    has_discussion_phrase = any(kw in body.lower() for kw in discussion_keywords) or any(kw in title.lower() for kw in discussion_keywords)

    if not (has_question or has_discussion_phrase):
        prompt = "What are your thoughts on this? Feedback and discussion are welcome."
        framed_body = f"{body.rstrip()}\n\n{prompt}".strip()
        applied.append({
            "type": "discussion_framing",
            "description": "Added discussion-first community prompt to body."
        })
        return framed_body, applied
    return body, applied


def tailor_for_destination(
    content_item: dict[str, Any],
    destination: dict[str, Any],
    *,
    connector: Any | None = None,
    manifest: dict[str, Any] | None = None,
    connection: dict[str, Any] | None = None,
    post_history: list[dict[str, Any] | str] | None = None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deterministically adapt a source content item for a single destination."""
    options = options or {}
    connection = connection or {}
    content = copy.deepcopy(content_item)
    original_content_ref = compute_content_ref(content_item)

    applied_transformations: list[dict[str, Any]] = []
    rejected_transformations: list[dict[str, Any]] = []
    warnings: list[str] = []
    validation_errors: list[str] = []

    destination_id = str(destination.get("id") or "")
    connector_key = str(destination.get("connector_key") or "")
    platform_key = str(destination.get("platform_key") or connector_key)
    surface_key = str(destination.get("surface") or "post")

    # Locate matching surface in manifest
    spec: dict[str, Any] = {}
    allowed_content_types: list[str] = ["text"]
    if manifest and manifest.get("surfaces"):
        matched_surface = next((s for s in manifest["surfaces"] if s.get("key") == surface_key), None)
        if matched_surface is None:
            matched_surface = manifest["surfaces"][0]
            warnings.append(f"Surface '{surface_key}' not found in manifest; using default '{matched_surface.get('key')}'.")
        spec = matched_surface.get("spec", {})
        allowed_content_types = matched_surface.get("content_types", ["text"])

    # Format limits validation
    title_max_chars = spec.get("title_max_chars")
    body_max_chars = spec.get("body_max_chars")
    title_min_chars = spec.get("title_min_chars")
    body_min_chars = spec.get("body_min_chars")

    if title_max_chars is not None and title_max_chars <= 0:
        warnings.append(f"Manifest title_max_chars is invalid ({title_max_chars}); ignoring limit.")
        title_max_chars = None
    if body_max_chars is not None and body_max_chars <= 0:
        warnings.append(f"Manifest body_max_chars is invalid ({body_max_chars}); ignoring limit.")
        body_max_chars = None
    if title_min_chars is not None and title_max_chars is not None and title_min_chars > title_max_chars:
        warnings.append("Manifest contains contradictory title length limits (min > max).")
    if body_min_chars is not None and body_max_chars is not None and body_min_chars > body_max_chars:
        warnings.append("Manifest contains contradictory body length limits (min > max).")

    # Content extraction
    title = str(content.get("title") or "")
    body = str(content.get("body") or "")
    canonical_url = content.get("canonical_url") or options.get("canonical_url")
    hashtags = list(content.get("hashtags") or [])
    mentions = list(content.get("mentions") or [])
    files = list(content.get("files") or [])
    is_promotional = bool(content.get("is_promotional", False))
    ai_generated = bool(content.get("ai_generated", False))
    content_lang = content.get("language")
    dest_lang = destination.get("language")

    # Empty or whitespace-only content validation
    if not title or not title.strip():
        validation_errors.append("Title cannot be empty or whitespace only.")
    if not body or not body.strip():
        validation_errors.append("Body cannot be empty or whitespace only.")

    # Language preservation check
    if content_lang and dest_lang and str(content_lang).lower() != str(dest_lang).lower():
        warnings.append(f"Content language '{content_lang}' differs from destination expected language '{dest_lang}'. Original language preserved.")

    # Media / content type selection
    requested_content_type = str(content.get("content_type") or "text")
    tailored_content_type = requested_content_type
    if requested_content_type not in allowed_content_types:
        if requested_content_type == "article" and "text" in allowed_content_types:
            tailored_content_type = "text"
            applied_transformations.append({
                "type": "content_type_adaptation",
                "description": "Adapted content type from 'article' to 'text'."
            })
        else:
            validation_errors.append(
                f"Content type '{requested_content_type}' is not supported by surface '{surface_key}' "
                f"(supported: {', '.join(allowed_content_types)})."
            )
            rejected_transformations.append({
                "transformation": "content_type_selection",
                "reason": f"Surface '{surface_key}' does not support '{requested_content_type}'."
            })

    if files and not any(ct in allowed_content_types for ct in ("image", "video_link", "binary")):
        validation_errors.append(
            f"Media files are not supported by surface '{surface_key}' "
            f"(supported: {', '.join(allowed_content_types)})."
        )
        rejected_transformations.append({
            "transformation": "file_attachments",
            "reason": f"Surface '{surface_key}' does not support media attachments."
        })

    # Rule checks: Self-promotion
    if is_promotional and not destination.get("self_promo_allowed", True):
        validation_errors.append("This destination does not allow self-promotion.")
        rejected_transformations.append({
            "transformation": "self_promotion",
            "reason": "Destination prohibits self-promotion."
        })

    # Rule checks: Automation
    if not destination.get("automation_allowed", True) and connector_key != "assisted":
        validation_errors.append("This destination requires assisted posting.")

    # Rule checks: Link policy & Canonical URL
    link_policy = str(destination.get("link_policy") or "allow")
    links_allowed = spec.get("links_allowed", True)

    if canonical_url:
        if link_policy == "block" or not links_allowed:
            rejected_transformations.append({
                "transformation": "canonical_url_inclusion",
                "reason": "Destination prohibits links (link_policy=block or links_allowed=false)."
            })
            validation_errors.append("Destination does not allow links, but canonical URL was provided.")
            canonical_url = None
        elif link_policy == "strip":
            canonical_url = None
            applied_transformations.append({
                "type": "link_policy_strip",
                "description": "Stripped canonical URL per destination link policy."
            })
        else:
            # link_policy == "allow"
            if connector_key in {"devto", "ghost"}:
                applied_transformations.append({
                    "type": "canonical_url_inclusion",
                    "description": f"Included native canonical URL for {connector_key}."
                })
            else:
                applied_transformations.append({
                    "type": "canonical_url_metadata",
                    "description": "Preserved canonical URL in tailored metadata."
                })

    content_with_links = f"{title}\n{body}"
    if LINK_RE.search(content_with_links):
        if link_policy == "block" or not links_allowed:
            validation_errors.append("This destination does not allow links.")
        elif link_policy == "strip":
            title = LINK_RE.sub("", title).strip()
            body = LINK_RE.sub("", body).strip()
            applied_transformations.append({
                "type": "link_policy_strip",
                "description": "Stripped inline links per destination link policy."
            })

    # Rule checks: AI disclosure
    if destination.get("disclosure_required") and ai_generated:
        if DISCLOSURE.lower() not in (title + "\n" + body).lower():
            if destination.get("disclosure_mode", "append") == "append":
                body = f"{body.rstrip()}\n\n{DISCLOSURE}.".strip()
                applied_transformations.append({
                    "type": "ai_disclosure",
                    "description": "Appended required AI-generated disclosure."
                })
            else:
                validation_errors.append("This destination requires an AI-content disclosure.")

    # Platform format adaptation: Forum discussion framing vs Article formatting
    is_forum = (
        surface_key in {"topic", "community_post", "forum", "thread"}
        or platform_key in {"discourse", "lemmy", "forum"}
        or connector_key in {"discourse", "lemmy"}
    )
    is_article = (
        surface_key in {"article", "post", "blog_post"}
        and (platform_key in {"devto", "ghost", "blog"} or connector_key in {"devto", "ghost"})
    )

    if is_forum:
        body, framing_applied = apply_discussion_framing(title, body)
        applied_transformations.extend(framing_applied)
    elif is_article:
        body, article_applied = apply_article_formatting(title, body)
        applied_transformations.extend(article_applied)

    # Hashtags and mentions
    formatted_tags, tag_warnings, tag_applied = format_hashtags(hashtags, platform_key)
    warnings.extend(tag_warnings)
    applied_transformations.extend(tag_applied)

    formatted_mentions, mention_warnings, mention_applied = format_mentions(mentions, platform_key)
    warnings.extend(mention_warnings)
    applied_transformations.extend(mention_applied)

    # Length limit enforcement (word boundary truncation)
    if title_max_chars and len(title) > title_max_chars:
        new_title, was_truncated, warn_msg = truncate_at_word_boundary(title, title_max_chars)
        if warn_msg:
            warnings.append(warn_msg)
        if was_truncated:
            applied_transformations.append({
                "type": "title_truncation",
                "original_length": len(title),
                "tailored_length": len(new_title),
                "limit": title_max_chars,
            })
            title = new_title

    if body_max_chars and len(body) > body_max_chars:
        new_body, was_truncated, warn_msg = truncate_at_word_boundary(body, body_max_chars)
        if warn_msg:
            warnings.append(warn_msg)
        if was_truncated:
            applied_transformations.append({
                "type": "body_truncation",
                "original_length": len(body),
                "tailored_length": len(new_body),
                "limit": body_max_chars,
            })
            body = new_body

    # Frequency cap check via posting-rules engine
    if destination.get("frequency_cap"):
        decision = check_rules(
            {"title": title, "body": body, "is_promotional": is_promotional, "ai_generated": ai_generated},
            destination,
            post_history or [],
        )
        if decision.outcome == "reschedule":
            validation_errors.append(decision.reason)

    # Form tailored content
    tailored_content = {
        "title": title,
        "body": body,
        "content_type": tailored_content_type,
        "canonical_url": canonical_url,
        "hashtags": formatted_tags,
        "mentions": formatted_mentions,
        "files": files,
        "is_promotional": is_promotional,
        "ai_generated": ai_generated,
    }
    if content_lang:
        tailored_content["language"] = content_lang

    # Connector authoritative validation
    if connector is not None and hasattr(connector, "validate"):
        rendition_for_val = {
            "title": title,
            "body": body,
            "canonical_url": canonical_url,
            "is_promotional": is_promotional,
            "ai_generated": ai_generated,
        }
        val = connector.validate(rendition_for_val, destination, connection)
        if not val.get("ok"):
            for violation in val.get("violations", []):
                msg = f"{violation.get('field', 'content')}: {violation.get('message', 'validation failed')}"
                if msg not in validation_errors:
                    validation_errors.append(msg)

    # Final readiness determination
    ready_for_publishing = len(validation_errors) == 0

    return {
        "destination_id": destination_id,
        "tailored_content": tailored_content,
        "applied_transformations": applied_transformations,
        "warnings": warnings,
        "rejected_transformations": rejected_transformations,
        "validation_results": {
            "ok": ready_for_publishing,
            "violations": [{"message": err} for err in validation_errors],
        },
        "validation_errors": validation_errors,
        "original_content_ref": original_content_ref,
        "ready_for_publishing": ready_for_publishing,
    }


class TailoringService:
    """Service for coordinating multi-destination content preview and job integration."""

    def __init__(self, session_factory, connector_registry=None):
        self.session_factory = session_factory
        self.connector_registry = connector_registry

    def preview(
        self,
        content_item: dict[str, Any],
        destination_ids: list[str],
        connections: dict[str, dict[str, Any]] | None = None,
        options: dict[str, Any] | None = None,
        *,
        progress: Any | None = None,
    ) -> dict[str, Any]:
        """Generate tailored renditions for multiple destinations."""
        connections = connections or {}
        options = options or {}
        content_ref = compute_content_ref(content_item)
        results: list[dict[str, Any]] = []

        for index, destination_id in enumerate(destination_ids, start=1):
            with self.session_factory() as session:
                destination = session.get(Destination, destination_id)
                if destination is None:
                    results.append({
                        "destination_id": destination_id,
                        "tailored_content": {},
                        "applied_transformations": [],
                        "warnings": [],
                        "rejected_transformations": [],
                        "validation_results": {
                            "ok": False,
                            "violations": [{"field": "destination_id", "message": "Destination was not found."}],
                        },
                        "validation_errors": ["Destination was not found."],
                        "original_content_ref": content_ref,
                        "ready_for_publishing": False,
                    })
                    if progress:
                        progress(int(index * 100 / len(destination_ids)))
                    continue

                destination_data = {column.name: getattr(destination, column.name) for column in Destination.__table__.columns}

            connector_key = destination_data.get("connector_key")
            connector = self.connector_registry.get(connector_key) if self.connector_registry else None
            manifest = self.connector_registry.manifests.get(connector_key) if self.connector_registry else None
            conn = connections.get(destination_id, {})

            outcome = tailor_for_destination(
                content_item,
                destination_data,
                connector=connector,
                manifest=manifest,
                connection=conn,
                options=options,
            )
            results.append(outcome)
            if progress:
                progress(int(index * 100 / len(destination_ids)))

        overall_ready = all(r["ready_for_publishing"] for r in results) if results else False
        return {
            "original_content_ref": content_ref,
            "destinations": results,
            "ready_for_publishing": overall_ready,
        }
