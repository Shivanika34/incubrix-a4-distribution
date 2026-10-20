"""CSV import, conservative relevance scoring, and safe optional reachability."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import io
import json
import re
import socket
import ipaddress
from typing import Callable
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import Destination, DestinationIdentity


REQUIRED_HEADERS = {"name", "url", "platform_key", "surface", "connector_key"}
ALLOWED_HEADERS = {
    "name", "url", "platform_key", "surface", "category", "audience_tags", "language", "geo",
    "est_audience", "rules_url", "submit_url", "frequency_cap", "self_promo_allowed", "link_policy",
    "disclosure_required", "automation_allowed", "connector_key", "relevance_score", "relevance_reason",
    "level", "last_certified_at", "description", "subject_area",
}
BOOL_FIELDS = {"self_promo_allowed", "disclosure_required", "automation_allowed"}
TARGETS = {
    "AI": {"ai", "artificial intelligence", "machine learning", "llm", "generative ai"},
    "Video": {"video", "youtube", "streaming", "creator video"},
    "Marketing": {"marketing", "growth", "seo", "advertising", "brand"},
    "SaaS": {"saas", "software as a service", "cloud software", "b2b software"},
    "Creators": {"creator", "creators", "content creation", "influencer"},
    "Founders": {"founder", "founders", "startup", "entrepreneur", "venture"},
}


class RowProblem(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def normalize_url(value: str) -> str:
    raw = value.strip()
    try:
        parsed = urlsplit(raw)
    except ValueError as error:
        raise RowProblem("INVALID_URL", "URL is malformed.") from error
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise RowProblem("INVALID_URL", "URL must be an HTTP(S) address without embedded credentials.")
    try:
        port = parsed.port
    except ValueError as error:
        raise RowProblem("INVALID_URL", "URL contains an invalid port.") from error
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise RowProblem("INVALID_URL", "URL host is malformed.") from error
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host if port is None or (parsed.scheme.lower(), port) in {("http", 80), ("https", 443)} else f"{host}:{port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower(), netloc, path, parsed.query, ""))


def score_relevance(record: dict) -> dict:
    """Score explicit text/tag evidence only; never infer audience size or engagement."""
    fields = ("name", "category", "description", "subject_area")
    text = " ".join(str(record.get(field) or "") for field in fields).casefold()
    tags_value = record.get("audience_tags") or []
    if isinstance(tags_value, str):
        tags_value = [part.strip() for part in re.split(r"[,;|]", tags_value) if part.strip()]
    tags = [str(tag).strip() for tag in tags_value if str(tag).strip()]
    searchable = text + " " + " ".join(tags).casefold()
    matched = []
    signals = []
    for category, terms in TARGETS.items():
        hits = sorted(term for term in terms if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", searchable))
        if hits:
            matched.append(category)
            signals.extend(hits[:2])
    if not searchable.strip():
        score, reason = 1, "Insufficient evidence: no name, category, description, subject area, or audience tags were supplied."
        warnings = ["Add descriptive audience or subject information before relying on this score."]
    elif not matched:
        score, reason = 1, "No explicit match to the AI, video, marketing, SaaS, creators, or founders audiences was found."
        warnings = ["Audience fit is uncertain because no target-category signals were found."]
    else:
        score = min(5, len(matched) + 2)
        reason = f"Matched {len(matched)} target audience categor{'y' if len(matched) == 1 else 'ies'}: {', '.join(matched)}."
        warnings = []
    if not tags:
        warnings.append("Audience tags were not provided; score relies on catalogue text only.")
    return {
        "relevance_score": score,
        "relevance_reason": reason,
        "matched_audiences": matched,
        "signals": signals,
        "warnings": warnings,
    }


def is_safe_public_url(url: str) -> bool:
    """Reject private/internal targets before optional network checks."""
    try:
        normalized = normalize_url(url)
        host = urlsplit(normalized).hostname
        addresses = set()
        if host:
            try:
                addresses = {ipaddress.ip_address(host)}
            except ValueError:
                pass
        if host and not addresses:
            addresses = {ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
        return bool(addresses) and all(address.is_global for address in addresses)
    except (RowProblem, OSError, ValueError):
        return False


@dataclass
class HttpReachabilityChecker:
    timeout_seconds: float = 3.0

    def __call__(self, url: str) -> dict:
        if not is_safe_public_url(url):
            return {"status": "blocked", "reason": "URL resolves to a private, local, or invalid address."}
        try:
            with httpx.Client(timeout=httpx.Timeout(self.timeout_seconds), follow_redirects=False) as client:
                response = client.head(url)
            if 300 <= response.status_code < 400:
                return {"status": "blocked", "reason": "Redirects are not followed during reachability checks."}
            return {"status": "reachable" if response.status_code < 500 else "unreachable", "http_status": response.status_code}
        except httpx.TimeoutException:
            return {"status": "unreachable", "reason": "Connection or response timed out."}
        except httpx.HTTPError:
            return {"status": "unreachable", "reason": "Connection check failed."}


def _parse_bool(value: str, field: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"true", "1", "yes", "y"}:
        return True
    if normalized in {"false", "0", "no", "n"}:
        return False
    raise RowProblem("INVALID_BOOLEAN", f"{field} must be true or false.")


def _parse_row(raw: dict[str, str], row_number: int) -> tuple[dict, dict]:
    row = {str(key or "").strip().casefold(): (value or "").strip() for key, value in raw.items()}
    if not any(row.values()):
        raise RowProblem("BLANK_ROW", "Blank CSV row was skipped.")
    data = {key: value for key, value in row.items() if key in ALLOWED_HEADERS}
    unknown = sorted(set(row) - ALLOWED_HEADERS)
    warnings = [f"Unknown columns ignored: {', '.join(unknown)}."] if unknown else []
    for field in REQUIRED_HEADERS:
        if not data.get(field):
            raise RowProblem("REQUIRED_VALUE_MISSING", f"{field} is required.")
    data["url"] = normalize_url(data["url"])
    for field in ("rules_url", "submit_url"):
        if data.get(field):
            data[field] = normalize_url(data[field])
    for field in BOOL_FIELDS:
        data[field] = _parse_bool(data[field], field) if data.get(field) else {"self_promo_allowed": True, "disclosure_required": False, "automation_allowed": True}[field]
    for field in ("audience_tags",):
        value = data.get(field, "")
        if value:
            try:
                parsed = json.loads(value)
                if value.startswith("[") and (not isinstance(parsed, list) or not all(isinstance(tag, str) for tag in parsed)):
                    raise RowProblem("INVALID_AUDIENCE_TAGS", "audience_tags JSON must be an array of strings.")
                data[field] = parsed if isinstance(parsed, list) else [part.strip() for part in re.split(r"[,;|]", value) if part.strip()]
            except json.JSONDecodeError:
                if value.startswith("["):
                    raise RowProblem("INVALID_AUDIENCE_TAGS", "audience_tags JSON is malformed.")
                data[field] = [part.strip() for part in re.split(r"[,;|]", value) if part.strip()]
        else:
            data[field] = []
    if data.get("frequency_cap"):
        try:
            data["frequency_cap"] = json.loads(data["frequency_cap"])
        except json.JSONDecodeError as error:
            raise RowProblem("INVALID_JSON", "frequency_cap must be a JSON object.") from error
        if not isinstance(data["frequency_cap"], dict):
            raise RowProblem("INVALID_JSON", "frequency_cap must be a JSON object.")
        if set(data["frequency_cap"]) - {"max_posts", "window_seconds"}:
            raise RowProblem("INVALID_FREQUENCY_CAP", "frequency_cap supports max_posts and window_seconds only.")
        for cap_key in ("max_posts", "window_seconds"):
            if cap_key in data["frequency_cap"]:
                if isinstance(data["frequency_cap"][cap_key], bool):
                    raise RowProblem("INVALID_FREQUENCY_CAP", f"frequency_cap.{cap_key} must be a positive integer.")
                try:
                    value = int(data["frequency_cap"][cap_key])
                except (TypeError, ValueError) as error:
                    raise RowProblem("INVALID_FREQUENCY_CAP", f"frequency_cap.{cap_key} must be a positive integer.") from error
                if value <= 0:
                    raise RowProblem("INVALID_FREQUENCY_CAP", f"frequency_cap.{cap_key} must be a positive integer.")
                data["frequency_cap"][cap_key] = value
    else:
        data["frequency_cap"] = {}
    if data.get("est_audience"):
        try:
            data["est_audience"] = int(data["est_audience"])
        except ValueError as error:
            raise RowProblem("INVALID_INTEGER", "est_audience must be a non-negative integer.") from error
        if not 0 <= data["est_audience"] <= 2**63 - 1:
            raise RowProblem("INVALID_INTEGER", "est_audience must be a non-negative 64-bit integer.")
    else:
        data["est_audience"] = None
    if data.get("relevance_score"):
        try:
            data["relevance_score"] = int(data["relevance_score"])
        except ValueError as error:
            raise RowProblem("INVALID_RELEVANCE_SCORE", "relevance_score must be an integer from 1 to 5.") from error
        if not 1 <= data["relevance_score"] <= 5:
            raise RowProblem("INVALID_RELEVANCE_SCORE", "relevance_score must be an integer from 1 to 5.")
    else:
        data["relevance_score"] = None
    if data.get("level") and not re.fullmatch(r"L[0-4]", data["level"]):
        raise RowProblem("INVALID_LEVEL", "level must be one of L0 through L4.")
    data.setdefault("level", "L0")
    if data.get("link_policy") and data["link_policy"] not in {"allow", "block", "strip"}:
        raise RowProblem("INVALID_LINK_POLICY", "link_policy must be allow, block, or strip.")
    data.setdefault("link_policy", "allow")
    if data.get("last_certified_at"):
        try:
            data["last_certified_at"] = datetime.fromisoformat(data["last_certified_at"].replace("Z", "+00:00"))
            if data["last_certified_at"].tzinfo is None:
                data["last_certified_at"] = data["last_certified_at"].replace(tzinfo=timezone.utc)
        except ValueError as error:
            raise RowProblem("INVALID_DATETIME", "last_certified_at must be an ISO-8601 timestamp.") from error
    else:
        data["last_certified_at"] = None
    for field in ("category", "language", "geo", "relevance_reason"):
        if not data.get(field):
            data[field] = None
    score = score_relevance(data)
    if data["relevance_score"] is None:
        data.update({key: score[key] for key in ("relevance_score", "relevance_reason")})
    else:
        data["relevance_reason"] = data.get("relevance_reason") or "Score supplied in source CSV; no independent audience validation was performed."
    data.pop("description", None)
    data.pop("subject_area", None)
    limits = {"name": 255, "url": 2048, "platform_key": 64, "surface": 64, "connector_key": 64, "category": 128, "language": 32, "geo": 64, "rules_url": 2048, "submit_url": 2048}
    for field, limit in limits.items():
        if data.get(field) is not None and len(str(data[field])) > limit:
            raise RowProblem("FIELD_TOO_LONG", f"{field} must be at most {limit} characters.")
    return data, score


class CatalogueImporter:
    def __init__(self, session_factory, *, max_rows: int = 5000, reachability_checker: Callable[[str], dict] | None = None, connector_keys: set[str] | None = None, connector_surfaces: dict[str, set[str]] | None = None):
        self.session_factory = session_factory
        self.max_rows = max_rows
        self.reachability_checker = reachability_checker
        self.connector_keys = connector_keys
        self.connector_surfaces = connector_surfaces

    def import_csv(self, content: str, progress: Callable[[int], None] | None = None) -> dict:
        result = {"rows_seen": 0, "accepted": [], "rejected": [], "duplicates": [], "warnings": [], "totals": {}}
        try:
            reader = csv.DictReader(io.StringIO(content, newline=""), strict=True)
            headers = {str(header or "").strip().casefold() for header in (reader.fieldnames or [])}
            missing = sorted(REQUIRED_HEADERS - headers)
            if missing:
                result["rejected"].append({"row": 1, "error_code": "REQUIRED_HEADER_MISSING", "message": f"Missing required headers: {', '.join(missing)}."})
                return self._totals(result)
            if len(headers) > len(ALLOWED_HEADERS) + 20:
                result["rejected"].append({"row": 1, "error_code": "TOO_MANY_COLUMNS", "message": "CSV has too many columns."})
                return self._totals(result)
            pending = []
            seen = {}
            for row_number, raw in enumerate(reader, start=2):
                result["rows_seen"] += 1
                if result["rows_seen"] > self.max_rows:
                    result["rejected"].append({"row": row_number, "error_code": "ROW_LIMIT_EXCEEDED", "message": f"Import is limited to {self.max_rows} data rows."})
                    break
                try:
                    data, scoring = _parse_row(raw, row_number)
                    if self.connector_keys is not None and data["connector_key"].casefold() not in self.connector_keys:
                        raise RowProblem("CONNECTOR_NOT_FOUND", f"Unknown connector_key '{data['connector_key']}'.")
                    if self.connector_surfaces is not None and data["surface"] not in self.connector_surfaces.get(data["connector_key"].casefold(), set()):
                        raise RowProblem("INVALID_SURFACE", f"Surface '{data['surface']}' is not supported by connector '{data['connector_key']}'.")
                    key = (data["url"], data["platform_key"].casefold())
                    if key in seen:
                        result["duplicates"].append({"row": row_number, "retained_row": seen[key], "url": data["url"], "platform_key": data["platform_key"], "error_code": "DUPLICATE_IN_IMPORT"})
                        continue
                    seen[key] = row_number
                    data["connector_key"] = data["connector_key"].casefold()
                    data["platform_key"] = data["platform_key"].casefold()
                    data["id"] = str(uuid4())
                    row_entry = {"row": row_number, "data": data, "warnings": list(scoring["warnings"]), "scoring": scoring}
                    if self.reachability_checker:
                        try:
                            check = self.reachability_checker(data["url"])
                            row_entry["reachability"] = check
                            if check.get("status") != "reachable":
                                row_entry["warnings"].append(f"Reachability: {check.get('status', 'unknown')}. {check.get('reason', '')}".strip())
                        except Exception:
                            row_entry["reachability"] = {"status": "unknown"}
                            row_entry["warnings"].append("Reachability check failed; status is unknown.")
                    pending.append(row_entry)
                except RowProblem as error:
                    if error.code == "BLANK_ROW":
                        result["warnings"].append({"row": row_number, "warning": error.code, "message": str(error)})
                    else:
                        result["rejected"].append({"row": row_number, "error_code": error.code, "message": str(error)})
                if progress and row_number % 25 == 0:
                    expected = max(1, content.count("\n"))
                    progress(min(90, int((row_number - 1) * 90 / expected)))

            with self.session_factory.begin() as session:
                existing = list(session.scalars(select(Destination)))
                existing_keys = {}
                for item in existing:
                    try:
                        existing_keys[(normalize_url(item.url), item.platform_key.casefold())] = item.id
                    except RowProblem:
                        # An invalid legacy URL cannot match a valid imported URL.
                        continue
                for item in pending:
                    data = item["data"]
                    key = (data["url"], data["platform_key"])
                    if key in existing_keys:
                        result["duplicates"].append({"row": item["row"], "retained_destination_id": existing_keys[key], "url": data["url"], "platform_key": data["platform_key"], "error_code": "DUPLICATE_EXISTING"})
                        continue
                    try:
                        with session.begin_nested():
                            session.add(Destination(**data))
                            session.add(DestinationIdentity(destination_id=data["id"], normalized_url=key[0], platform_key=key[1]))
                            session.flush()
                    except IntegrityError:
                        identity = session.scalar(select(DestinationIdentity).where(DestinationIdentity.normalized_url == key[0], DestinationIdentity.platform_key == key[1]))
                        if identity is None:
                            raise
                        result["duplicates"].append({"row": item["row"], "retained_destination_id": identity.destination_id, "url": data["url"], "platform_key": data["platform_key"], "error_code": "DUPLICATE_EXISTING"})
                        continue
                    existing_keys[key] = data["id"]
                    result["accepted"].append({"row": item["row"], "destination_id": data["id"], "url": data["url"], "platform_key": data["platform_key"], "relevance_score": data["relevance_score"], "relevance_reason": data["relevance_reason"], "matched_audiences": item["scoring"]["matched_audiences"], "signals": item["scoring"]["signals"], "warnings": item["warnings"], "reachability": item.get("reachability", {"status": "unknown"})})
        except csv.Error as error:
            result["rejected"].append({"row": result["rows_seen"] + 2, "error_code": "MALFORMED_CSV", "message": f"Malformed CSV: {error}."})
        return self._totals(result)

    @staticmethod
    def _totals(result: dict) -> dict:
        result["totals"] = {
            "rows_seen": result["rows_seen"], "accepted": len(result["accepted"]), "rejected": len(result["rejected"]),
            "duplicates": len(result["duplicates"]), "warnings": len(result["warnings"]) + sum(len(item.get("warnings", [])) for item in result["accepted"]),
        }
        return result
