"""Signed callback adapter for completed jobs."""

import hashlib
import hmac
import json
from typing import Protocol

import httpx


class CallbackAdapter(Protocol):
    def post(self, url: str, payload: dict) -> None: ...


class HttpCallbackAdapter:
    def __init__(self, secret: str):
        self.secret = secret.encode("utf-8")

    def post(self, url: str, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        signed = hmac.new(self.secret, body, hashlib.sha256).hexdigest()
        with httpx.Client(timeout=10.0) as client:
            client.post(url, content=body, headers={"Content-Type": "application/json", "X-IncuBrix-Signature": signed}).raise_for_status()
