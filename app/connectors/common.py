"""Shared validation and platform-error behaviour for connector implementations."""

from typing import Any, Callable
import time

from app.connectors.http import PlatformHttpAdapter, PlatformResponse
from app.errors import AppError, ERROR_CODES


def validate_rendition(manifest: dict[str, Any], rendition: dict[str, Any]) -> dict[str, Any]:
    spec = manifest["surfaces"][0]["spec"]
    violations = []
    for field, limit_key in (("title", "title_max_chars"), ("body", "body_max_chars")):
        limit = spec.get(limit_key)
        if limit is not None and rendition.get(field) and len(rendition[field]) > limit:
            violations.append({"field": field, "message": f"must be at most {limit} characters"})
    return {"ok": not violations, "violations": violations}


def _map_platform_error(status_code: int, error_map: dict[str, str]) -> str:
    return error_map.get(str(status_code), "PLATFORM_UNAVAILABLE" if status_code >= 500 else "PLATFORM_CONTENT_REJECTED")


def request_with_backoff(adapter: PlatformHttpAdapter, method: str, url: str, *, headers: dict[str, str], payload: dict | None, manifest: dict, sleep: Callable[[float], None] | None = None) -> PlatformResponse:
    """Retry once after a platform-provided 429 reset time; never invent a delay."""
    sleeper = sleep or time.sleep
    response = adapter.request(method, url, headers=headers, json=payload)
    if response.status_code == 429:
        reset = response.headers.get("X-RateLimit-Reset", response.headers.get("Retry-After", "1"))
        try:
            delay = max(0, float(reset))
        except ValueError:
            delay = 1
        sleeper(delay)
        response = adapter.request(method, url, headers=headers, json=payload)
    if 200 <= response.status_code < 300:
        return response
    code = _map_platform_error(response.status_code, manifest["error_map"])
    message = response.body.get("error") or response.body.get("message") or f"Platform returned HTTP {response.status_code}."
    raise AppError(code, str(message), ERROR_CODES.get(code, response.status_code >= 500), {"platform_status": response.status_code})
