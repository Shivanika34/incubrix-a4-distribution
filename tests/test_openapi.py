import json
from pathlib import Path

from app.main import app


def test_committed_openapi_matches_running_app():
    expected = json.dumps(app.openapi(), indent=2, ensure_ascii=False) + "\n"
    path = Path(__file__).resolve().parents[1] / "openapi.yaml"
    assert path.read_text(encoding="utf-8") == expected
    assert app.openapi()["openapi"].startswith("3.1.")
