from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
import threading
import time

from fastapi.testclient import TestClient

from app.callbacks import HttpCallbackAdapter
from app.main import create_app


def wait_for_completion(client, job_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200
        job = response.json()
        if job["status"] in {"succeeded", "failed"}:
            return job
        time.sleep(0.01)
    raise AssertionError("job did not finish before the test deadline")


def test_job_progresses_from_queued_to_success(client):
    response = client.post("/api/v1/jobs/echo", headers={"Idempotency-Key": "progress-1"}, json={"text": "hello"})
    assert response.status_code == 202
    first = response.json()
    assert first["status"] in {"queued", "running", "succeeded"}
    final = wait_for_completion(client, first["job_id"])
    assert final["status"] == "succeeded"
    assert final["progress"] == 100
    assert final["result_links"]
    assert final["created_at"] and final["updated_at"]


def test_same_key_and_same_body_returns_same_job_and_runs_once(client, app):
    first = client.post("/api/v1/jobs/echo", headers={"Idempotency-Key": "same-1"}, json={"text": "same"})
    second = client.post("/api/v1/jobs/echo", headers={"Idempotency-Key": "same-1"}, json={"text": "same"})
    assert first.status_code == second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    wait_for_completion(client, first.json()["job_id"])
    assert app.state.jobs.work_count == 1


def test_same_key_with_different_body_is_rejected(client):
    client.post("/api/v1/jobs/echo", headers={"Idempotency-Key": "reuse-1"}, json={"text": "first"})
    response = client.post("/api/v1/jobs/echo", headers={"Idempotency-Key": "reuse-1"}, json={"text": "second"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_parallel_requests_with_same_key_create_one_job(app):
    barrier = threading.Barrier(2)

    def send_request(client):
        barrier.wait()
        return client.post("/api/v1/jobs/echo", headers={"Idempotency-Key": "parallel-1"}, json={"text": "one"})

    # Keep one lifespan open so the shared worker pool is not shut down mid-test.
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(send_request, client) for _ in range(2)]
        responses = [future.result(timeout=5) for future in futures]
        ids = {response.json()["job_id"] for response in responses}
        assert all(response.status_code == 202 for response in responses)
        assert len(ids) == 1
        wait_for_completion(client, ids.pop())
        assert app.state.jobs.work_count == 1


class RecordingCallback:
    def __init__(self):
        self.event = threading.Event()
        self.received = None

    def post(self, url, payload):
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        self.received = (url, payload)
        self.event.set()


def test_callback_is_sent_after_job_finishes(settings):
    callback = RecordingCallback()
    app = create_app(settings=settings, callback_adapter=callback)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/jobs/echo",
            headers={"Idempotency-Key": "callback-1"},
            json={"text": "notify", "callback_url": "https://callback.example/result"},
        )
        assert response.status_code == 202
        job = wait_for_completion(client, response.json()["job_id"])
        assert job["status"] == "succeeded"
        assert callback.event.wait(1)
    url, payload = callback.received
    assert url == "https://callback.example/result"
    assert payload["status"] == "succeeded"


def test_callback_adapter_signs_exact_post_body(monkeypatch):
    observed = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, timeout):
            observed["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, url, content, headers):
            observed.update(url=url, content=content, headers=headers)
            return FakeResponse()

    monkeypatch.setattr("app.callbacks.httpx.Client", FakeClient)
    payload = {"job_id": "job-123", "status": "succeeded", "progress": 100}
    HttpCallbackAdapter("test-callback-signing-key").post("https://callback.example/result", payload)
    expected_body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    expected_signature = hmac.new(b"test-callback-signing-key", expected_body, hashlib.sha256).hexdigest()
    assert observed["content"] == expected_body
    assert observed["headers"]["X-IncuBrix-Signature"] == expected_signature
