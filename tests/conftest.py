import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

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
    )


@pytest.fixture
def app(settings):
    return create_app(settings=settings, readiness_adapter=ReadyAdapter())


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client
