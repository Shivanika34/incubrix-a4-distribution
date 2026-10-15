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
}


@dataclass
class AppError(Exception):
    code: str
    message: str
    retryable: bool
    details: Any = None

    def __str__(self) -> str:
        return self.message
