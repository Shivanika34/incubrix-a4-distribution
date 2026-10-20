"""Shared validation and platform-error behaviour for connector implementations."""

from typing import Any, Callable
import time
import httpx
from urllib.parse import urlsplit

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
    try:
        response = adapter.request(method, url, headers=headers, json=payload)
    except (httpx.ConnectTimeout, httpx.PoolTimeout) as error:
        raise AppError("PLATFORM_UNAVAILABLE", "The platform connection timed out before publication could be confirmed.", True) from error
    except httpx.RequestError as error:
        # Once a request has been sent, transport failures cannot prove whether
        # the remote platform accepted it. Avoid turning those into blind retries.
        if isinstance(error, httpx.ConnectError):
            raise AppError("PLATFORM_UNAVAILABLE", "Could not establish a connection to the platform.", True) from error
        method_name = method.upper()
        if method_name in {"GET", "HEAD", "OPTIONS"}:
            raise AppError("PLATFORM_UNAVAILABLE", "The platform response could not be read.", True) from error
        is_authentication = urlsplit(url).path.endswith("/user/login")
        code = "PUBLISH_OUTCOME_UNKNOWN" if method_name == "POST" and not is_authentication else "PLATFORM_OUTCOME_UNKNOWN"
        if isinstance(error, (httpx.ReadTimeout, httpx.WriteTimeout)):
            message = "The publication request timed out after it may have reached the platform. Check the platform before retrying." if code == "PUBLISH_OUTCOME_UNKNOWN" else "The platform request timed out after it may have reached the platform. Verify its result before retrying."
        else:
            message = "The publication request failed after it may have reached the platform. Check the platform before retrying." if code == "PUBLISH_OUTCOME_UNKNOWN" else "The platform request failed after it may have reached the platform. Verify its result before retrying."
        raise AppError(
            code,
            message,
            True,
        ) from error
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
    # Access-denial responses often include proxy/CDN diagnostics. Keep their
    # bodies out of API errors and logs so they cannot reflect credentials or
    # other request details back to callers.
    if response.status_code == 403:
        message = "Platform denied the request (HTTP 403)."
    else:
        message = response.body.get("error") or response.body.get("message") or f"Platform returned HTTP {response.status_code}."
    raise AppError(code, str(message), ERROR_CODES.get(code, response.status_code >= 500), {"platform_status": response.status_code})
