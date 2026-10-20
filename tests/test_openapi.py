import json
from pathlib import Path

from app.main import app


def test_committed_openapi_matches_running_app():
    expected = json.dumps(app.openapi(), indent=2, ensure_ascii=False) + "\n"
    path = Path(__file__).resolve().parents[1] / "openapi.yaml"
    assert path.read_text(encoding="utf-8") == expected
    assert app.openapi()["openapi"].startswith("3.1.")
    for route in ("/api/v1/publishing-orchestrations", "/api/v1/publishing-orchestrations/preview"):
        parameters = app.openapi()["paths"][route]["post"]["parameters"]
        key_header = next(item for item in parameters if item["name"] == "Idempotency-Key")
        assert key_header["required"] is True


def test_every_post_route_documents_required_idempotency_key():
    for path, operations in app.openapi()["paths"].items():
        if "post" not in operations:
            continue
        parameters = operations["post"].get("parameters", [])
        key_header = next((item for item in parameters if item["name"] == "Idempotency-Key"), None)
        assert key_header is not None, path
        assert key_header["required"] is True, path
