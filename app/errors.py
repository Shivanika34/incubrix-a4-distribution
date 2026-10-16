"""Shared error shape and code registry."""

from dataclasses import dataclass
from typing import Any


ERROR_CODES: dict[str, bool] = {
    "INTERNAL_ERROR": True,
    "SERVICE_NOT_READY": True,
    "IDEMPOTENCY_KEY_REQUIRED": False,
    "IDEMPOTENCY_KEY_REUSED": False,
    "JOB_NOT_FOUND": False,
    "REQUEST_VALIDATION_ERROR": False,
    "HTTP_ERROR": False,
    "DEST_SPEC_VIOLATION": False,
    "DEST_RULE_FREQUENCY_CAP": True,
    "CONNECTION_AUTH_EXPIRED": True,
    "PLATFORM_RATE_LIMIT": True,
    "PLATFORM_QUOTA_EXHAUSTED": True,
    "PLATFORM_CONTENT_REJECTED": False,
    "PLATFORM_UNAVAILABLE": True,
    "CONNECTOR_DISABLED": False,
    "CONNECTOR_NOT_FOUND": False,
    "CONNECTOR_METHOD_NOT_FOUND": False,
    "DESTINATION_NOT_FOUND": False,
}


@dataclass
class AppError(Exception):
    code: str
    message: str
    retryable: bool
    details: Any = None

    def __str__(self) -> str:
        return self.message
