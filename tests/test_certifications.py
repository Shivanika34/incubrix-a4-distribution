import time
from dataclasses import replace
import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import CertificationRun, Destination
from app.main import create_app


def wait_for_job(client, job_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "failed"}:
            return job
        time.sleep(0.01)
    raise AssertionError("certification job did not finish")


def create_destination(client, connector_key="noop"):
    response = client.post(
        "/api/v1/destinations",
        json={
            "name": "Certification destination",
            "url": "https://destination.example.test/submit",
            "platform_key": connector_key,
            "surface": "post",
            "connector_key": connector_key,
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_certification_publishes_checks_status_deletes_and_persists_evidence(client, app):
    destination_id = create_destination(client)
    response = client.post(
        "/api/v1/certifications",
        headers={"Idempotency-Key": "certification-1"},
        json={"destination_id": destination_id},
    )

    assert response.status_code == 202
    job = wait_for_job(client, response.json()["job_id"])
    evidence = job["result"]["certification"]

    assert job["status"] == "succeeded"
    assert evidence["status"] == "certified"
    assert evidence["status_result"]["status"] == "live"
    assert evidence["delete_result"]["deleted"] is True
    with app.state.session_factory() as session:
        run = session.scalar(select(CertificationRun).where(CertificationRun.id == evidence["certification_run_id"]))
        destination = session.get(Destination, destination_id)
        assert run.status == "certified"
        assert run.completed_at is not None
        assert destination.last_certified_at is not None
        assert destination.level == "L3"
    artifact = Path("artifacts/certification/noop") / f"{evidence['certification_run_id']}.json"
    saved = json.loads(artifact.read_text(encoding="utf-8"))
    assert saved["status"] == "certified"
    assert saved["delete_result"]["deleted"] is True


def test_certification_is_idempotent(client, app):
    destination_id = create_destination(client)
    payload = {"destination_id": destination_id}
    first = client.post("/api/v1/certifications", headers={"Idempotency-Key": "certification-duplicate"}, json=payload)
    second = client.post("/api/v1/certifications", headers={"Idempotency-Key": "certification-duplicate"}, json=payload)

    assert first.json()["job_id"] == second.json()["job_id"]
    job = wait_for_job(client, first.json()["job_id"])
    connector = app.state.connector_registry.get("noop")
    assert job["result"]["certification"]["status"] == "certified"
    assert connector.posts == {}


def test_assisted_certification_records_pack_without_fake_publish(client, app):
    response = client.post(
        "/api/v1/destinations",
        json={
            "name": "Assisted certification",
            "url": "https://destination.example.test/submit",
            "submit_url": "https://destination.example.test/submit",
            "platform_key": "assisted",
            "surface": "post",
            "connector_key": "assisted",
        },
    )
    destination_id = response.json()["id"]
    submitted = client.post(
        "/api/v1/certifications",
        headers={"Idempotency-Key": "certification-assisted"},
        json={"destination_id": destination_id},
    )

    job = wait_for_job(client, submitted.json()["job_id"])
    evidence = job["result"]["certification"]
    assert evidence["status"] == "assisted"
    assert evidence["posting_pack"]["deep_link"] == "https://destination.example.test/submit"
    with app.state.session_factory() as session:
        destination = session.get(Destination, destination_id)
        assert destination.level == "L1"


def test_disabled_connector_fails_certification_without_publish(settings):
    app = create_app(settings=replace(settings, connector_disabled=frozenset({"noop"})))
    with TestClient(app) as client:
        destination_id = create_destination(client)
        response = client.post(
            "/api/v1/certifications",
            headers={"Idempotency-Key": "certification-disabled"},
            json={"destination_id": destination_id},
        )
        job = wait_for_job(client, response.json()["job_id"])
        assert job["status"] == "failed"
        assert job["result"]["certification"]["error_code"] == "CONNECTOR_DISABLED"
