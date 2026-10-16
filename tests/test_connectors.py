from dataclasses import replace

from fastapi.testclient import TestClient
import pytest

from app.connectors.registry import map_platform_error, validate_manifest
from app.main import create_app


def destination(name="Example forum"):
    return {
        "name": name,
        "url": "https://forum.example.test",
        "platform_key": "example",
        "surface": "post",
        "connector_key": "noop",
    }


def test_manifest_validation_rejects_missing_contract_fields():
    with pytest.raises(ValueError, match="Invalid connector manifest"):
        validate_manifest({"key": "broken"})


def test_error_mapping_uses_manifest_mapping():
    errors = {"401": "CONNECTION_AUTH_EXPIRED", "429": "PLATFORM_RATE_LIMIT"}
    assert map_platform_error(401, errors) == "CONNECTION_AUTH_EXPIRED"
    assert map_platform_error(429, errors) == "PLATFORM_RATE_LIMIT"
    assert map_platform_error(503, errors) == "PLATFORM_UNAVAILABLE"


def test_connector_dispatch_validate_publish_status_and_delete(client):
    payload = {"rendition": {"title": "Hello", "body": "A post"}, "destination": destination(), "idempotency_key": "post-1"}
    validated = client.post("/connector/v1/noop/validate", json=payload)
    assert validated.status_code == 200 and validated.json() == {"ok": True, "violations": []}

    published = client.post("/connector/v1/noop/publish", json=payload)
    assert published.status_code == 200
    post_id = published.json()["platform_post_id"]
    assert client.post("/connector/v1/noop/get_status", json={"platform_post_id": post_id}).json()["status"] == "live"
    assert client.post("/connector/v1/noop/delete", json={"platform_post_id": post_id}).json() == {"deleted": True}


def test_unknown_connector_and_method_use_standard_errors(client):
    unknown = client.post("/connector/v1/missing/publish", json={})
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "CONNECTOR_NOT_FOUND"
    method = client.post("/connector/v1/noop/missing", json={})
    assert method.status_code == 400
    assert method.json()["error"]["code"] == "CONNECTOR_METHOD_NOT_FOUND"


def test_connector_kill_switch(settings):
    app = create_app(settings=replace(settings, connector_disabled=frozenset({"noop"})))
    with TestClient(app) as client:
        response = client.post("/connector/v1/noop/publish", json={"rendition": {}, "destination": destination()})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CONNECTOR_DISABLED"


def test_two_destinations_use_one_connector_without_code_change(client):
    first = client.post("/api/v1/destinations", json=destination("Forum one"))
    second = client.post("/api/v1/destinations", json={**destination("Forum two"), "url": "https://two.example.test"})
    assert first.status_code == second.status_code == 201
    rows = client.get("/api/v1/destinations").json()
    assert [row["name"] for row in rows] == ["Forum one", "Forum two"]
