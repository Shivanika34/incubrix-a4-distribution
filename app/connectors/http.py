"""Small HTTP adapter boundary. Real connectors use it; tests provide a fake adapter."""

from dataclasses import dataclass
from typing import Any, Protocol
import httpx


@dataclass(frozen=True)
class PlatformResponse:
    status_code: int
    body: dict[str, Any]
    headers: dict[str, str]


class PlatformHttpAdapter(Protocol):
    def request(self, method: str, url: str, *, headers: dict[str, str], json: dict[str, Any] | None = None) -> PlatformResponse: ...


class HttpxPlatformHttpAdapter:
    """The only adapter that makes outgoing HTTP calls in production mode."""

    def __init__(self, timeout_seconds: float = 10):
        self.timeout_seconds = timeout_seconds

    def request(self, method: str, url: str, *, headers: dict[str, str], json: dict[str, Any] | None = None) -> PlatformResponse:
        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.request(method, url, headers=headers, json=json)
        try:
            body = response.json()
        except ValueError:
            body = {"message": response.text}
        return PlatformResponse(response.status_code, body if isinstance(body, dict) else {"data": body}, dict(response.headers))
