"""Run the public Render demo as one free web service with an internal mock."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request


def configure_demo_environment() -> None:
    """Force demo-safe settings before importing the API or connector registry."""
    values = {
        "SIMULATION_MODE": "true",
        "MOCK_PLATFORM_URL": "http://127.0.0.1:10001",
        "DATABASE_URL": "sqlite:////tmp/incubrix-demo.sqlite3",
        "DEMO_EPHEMERAL_ARTIFACTS": "true",
        "DEMO_ARTIFACT_DIRECTORY": "/tmp/incubrix-artifacts",
        "MINIO_ENDPOINT": "http://127.0.0.1:9",
        "MINIO_ACCESS_KEY": "",
        "MINIO_SECRET_KEY": "",
        "GHOST_REAL_ADMIN_API_KEY": "",
        "DEVTO_REAL_API_KEY": "",
        "DEVTO_REAL_USERNAME": "",
        "DISCOURSE_REAL_API_KEY": "",
        "DISCOURSE_REAL_API_USERNAME": "",
        "LEMMY_REAL_USERNAME": "",
        "LEMMY_REAL_PASSWORD": "",
        "LEMMY_LOCAL_TEST_DESTINATION_ID": "",
    }
    os.environ.update(values)


def wait_for_mock(process: subprocess.Popen, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("The internal simulation service exited during startup.")
        try:
            with urllib.request.urlopen("http://127.0.0.1:10001/health", timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("The internal simulation service did not become ready in time.")


def main() -> None:
    configure_demo_environment()
    mock = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.mock_platform:app", "--host", "127.0.0.1", "--port", "10001", "--no-access-log"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for_mock(mock)
        import uvicorn

        uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")), proxy_headers=True)
    finally:
        mock.terminate()
        try:
            mock.wait(timeout=5)
        except subprocess.TimeoutExpired:
            mock.kill()
            mock.wait(timeout=5)


if __name__ == "__main__":
    main()
