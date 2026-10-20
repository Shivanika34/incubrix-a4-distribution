import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


class FakeArtifactStore:
    def __init__(self):
        self.objects = {}
        self.fail = False

    def put_bytes(self, key, data, content_type):
        if self.fail:
            from app.errors import AppError
            raise AppError("ARTIFACT_STORAGE_FAILED", "mock storage failure", True)
        self.objects[key] = (data, content_type)
        return {
            "uri": f"s3://test-bucket/{key}",
            "download_url": f"https://storage.test/{key}",
            "content_type": content_type,
            "size_bytes": len(data),
        }


class FakeContentFetcher:
    def __init__(self):
        self.calls = []

    def fetch(self, url, max_bytes):
        self.calls.append((url, max_bytes))
        return b'{"content_item":{"title":"Remote","body":"Fetched","license":"CC-BY"}}', "application/json", url

# The restricted Windows runner blocks the Proactor loop's socketpair accept.
if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class ReadyAdapter:
    def __init__(self, should_fail=False):
        self.should_fail = should_fail

    def check(self):
        if self.should_fail:
            raise RuntimeError("dependency unavailable")


@pytest.fixture
def settings():
    db_path = Path.cwd() / ".pytest-tmp" / f"{uuid4().hex}.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return Settings(
        database_url=f"sqlite:///{db_path}",
        minio_endpoint="http://localhost:9000",
        minio_access_key="",
        minio_secret_key="",
        minio_bucket="test-bucket",
        log_level="INFO",
        simulation_mode=True,
        default_rate_limit_requests=60,
        default_rate_limit_window_seconds=60,
        app_config_file="",
        callback_hmac_secret="test-callback-signing-key",
        echo_sleep_seconds=0.02,
        connectors_dir="connectors",
        connector_disabled=frozenset(),
        mock_platform_url="http://mock-platform:8080",
        webhook_secrets={},
        certification_evidence_dir="artifacts/certification",
        catalogue_import_max_bytes=2097152,
        catalogue_import_max_rows=5000,
        catalogue_reachability_enabled=False,
    )


@pytest.fixture
def app(settings):
    return create_app(
        settings=settings,
        readiness_adapter=ReadyAdapter(),
        artifact_store=FakeArtifactStore(),
        content_fetcher=FakeContentFetcher(),
    )


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client
