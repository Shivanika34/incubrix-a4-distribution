import os
from pathlib import Path

import yaml

from app.artifacts import EphemeralArtifactStore
from app.errors import AppError
from app import render_demo
from app.render_demo import configure_demo_environment


def test_ephemeral_artifact_store_serves_written_artifacts_and_rejects_traversal(tmp_path):
    store = EphemeralArtifactStore(str(tmp_path), "https://demo.example")
    saved = store.put_bytes("results/sample.json", b'{"ok":true}', "application/json")
    assert saved["download_url"] == "https://demo.example/api/v1/demo-artifacts/results/sample.json"
    data, content_type = store.get_bytes("results/sample.json")
    assert data == b'{"ok":true}'
    assert content_type == "application/json"
    try:
        store.get_bytes("../secret")
    except AppError as error:
        assert error.code == "ARTIFACT_NOT_FOUND"
    else:
        raise AssertionError("path traversal must not expose files outside artifact storage")


def test_render_demo_forces_simulation_and_clears_platform_credentials(monkeypatch):
    monkeypatch.setenv("SIMULATION_MODE", "false")
    monkeypatch.setenv("GHOST_REAL_ADMIN_API_KEY", "configured-secret")
    monkeypatch.setenv("DEVTO_REAL_API_KEY", "configured-secret")
    monkeypatch.setenv("DISCOURSE_REAL_API_KEY", "configured-secret")
    monkeypatch.setenv("LEMMY_REAL_PASSWORD", "configured-secret")

    configure_demo_environment()

    assert os.environ["SIMULATION_MODE"] == "true"
    assert os.environ["GHOST_REAL_ADMIN_API_KEY"] == ""
    assert os.environ["DEVTO_REAL_API_KEY"] == ""
    assert os.environ["DISCOURSE_REAL_API_KEY"] == ""
    assert os.environ["LEMMY_REAL_PASSWORD"] == ""
    assert os.environ["MOCK_PLATFORM_URL"] == "http://127.0.0.1:10001"


def test_render_blueprint_uses_existing_simulation_launcher_and_health_check():
    root = Path(__file__).resolve().parents[1]
    blueprint = yaml.safe_load((root / "render.yaml").read_text(encoding="utf-8"))
    service = blueprint["services"][0]

    assert service["type"] == "web"
    assert service["runtime"] == "docker"
    assert service["dockerCommand"] == "python -m app.render_demo"
    assert service["healthCheckPath"] == "/health"
    assert (root / "app" / "render_demo.py").is_file()
    assert "COPY web ./web" in (root / "Dockerfile").read_text(encoding="utf-8")
    assert '@application.get("/", include_in_schema=False)' in (
        root / "app" / "main.py"
    ).read_text(encoding="utf-8")
    assert {item["key"]: item.get("value") for item in service["envVars"]}["SIMULATION_MODE"] == "true"


def test_render_launcher_binds_to_render_port_and_public_host(monkeypatch):
    class MockProcess:
        terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    process = MockProcess()
    calls = {}
    monkeypatch.setenv("PORT", "17345")
    monkeypatch.setattr(render_demo.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(render_demo, "wait_for_mock", lambda _process: None)
    monkeypatch.setattr(
        "uvicorn.run",
        lambda app, **kwargs: calls.update({"app": app, **kwargs}),
    )

    render_demo.main()

    assert calls["app"] == "app.main:app"
    assert calls["host"] == "0.0.0.0"
    assert calls["port"] == 17345
    assert process.terminated


def test_demo_homepage_health_readiness_and_openapi_routes(client):
    homepage = client.get("/")
    assert homepage.status_code == 200
    assert "IncuBrix publishing demo" in homepage.text

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}

    readiness = client.get("/ready")
    assert readiness.status_code == 200
    assert readiness.json() == {"status": "ready"}

    openapi = client.get("/openapi.json")
    assert openapi.status_code == 200
    assert openapi.json()["info"]["title"] == "IncuBrix Long-Tail Distribution"
