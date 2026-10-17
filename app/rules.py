"""Pure destination posting-rule evaluation used before every connector publish."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Any

LINK_RE = re.compile(r"https?://\S+", re.IGNORECASE)
DISCLOSURE = "AI-generated"


@dataclass(frozen=True)
class RuleResult:
    outcome: str
    reason: str
    error_code: str | None = None
    retry_at: str | None = None
    rendition: dict[str, Any] | None = None


def _timestamp(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def check_rules(rendition: dict[str, Any], destination: dict[str, Any], post_history: list[dict[str, Any] | str], now: datetime | None = None) -> RuleResult:
    """Return an allow, block, or reschedule decision without database or network access."""
    now = now or datetime.now(timezone.utc)
    cap = destination.get("frequency_cap") or {}
    if cap.get("max_posts") is not None and cap.get("window_seconds"):
        cutoff = now - timedelta(seconds=int(cap["window_seconds"]))
        recent = [_timestamp(item.get("posted_at")) if isinstance(item, dict) else _timestamp(item) for item in post_history]
        recent = [posted for posted in recent if posted > cutoff]
        if len(recent) >= int(cap["max_posts"]):
            retry_at = min(recent) + timedelta(seconds=int(cap["window_seconds"]))
            return RuleResult("reschedule", "Frequency cap reached for this destination.", "DEST_RULE_FREQUENCY_CAP", retry_at.isoformat())

    if rendition.get("is_promotional") and not destination.get("self_promo_allowed", True):
        return RuleResult("blocked", "This destination does not allow self-promotion.", "DEST_SPEC_VIOLATION")

    editable = dict(rendition)
    content = f"{editable.get('title', '')}\n{editable.get('body', '')}"
    if LINK_RE.search(content) and destination.get("link_policy", "allow") != "allow":
        if destination.get("link_policy") == "strip":
            editable["title"] = LINK_RE.sub("", editable.get("title", "")).strip()
            editable["body"] = LINK_RE.sub("", editable.get("body", "")).strip()
            return RuleResult("allowed", "Links were removed to meet this destination's link policy.", rendition=editable)
        return RuleResult("blocked", "This destination does not allow links.", "DEST_SPEC_VIOLATION")

    if destination.get("disclosure_required") and rendition.get("ai_generated") and DISCLOSURE.lower() not in content.lower():
        if destination.get("disclosure_mode", "block") == "append":
            editable["body"] = f"{editable.get('body', '').rstrip()}\n\n{DISCLOSURE}.".strip()
            return RuleResult("allowed", "AI-content disclosure was added.", rendition=editable)
        return RuleResult("blocked", "This destination requires an AI-content disclosure.", "DEST_SPEC_VIOLATION")

    if not destination.get("automation_allowed", True):
        return RuleResult("blocked", "This destination requires assisted posting.", "DEST_SPEC_VIOLATION")
    return RuleResult("allowed", "All destination posting rules passed.", rendition=editable)
