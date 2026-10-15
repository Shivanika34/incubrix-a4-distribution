"""Adapters for readiness checks and job-completion callbacks."""

import hashlib
from typing import Protocol

from minio import Minio
from sqlalchemy import text

from app.config import Settings


class ReadinessAdapter(Protocol):
    def check(self) -> None: ...


class ServiceReadiness:
    def __init__(self, session_factory, settings: Settings):
        self.session_factory = session_factory
        self.settings = settings

    def check(self) -> None:
        with self.session_factory() as session:
            session.execute(text("SELECT 1"))
        client = Minio(
            self.settings.minio_endpoint.removeprefix("http://").removeprefix("https://"),
            access_key=self.settings.minio_access_key or None,
            secret_key=self.settings.minio_secret_key or None,
            secure=self.settings.minio_endpoint.startswith("https://"),
        )
        client.bucket_exists(self.settings.minio_bucket)


