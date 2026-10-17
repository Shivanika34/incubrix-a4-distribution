"""Refresh the committed OpenAPI document from the running application schema."""

import json
from pathlib import Path

from app.main import app


def main() -> None:
    output = Path(__file__).resolve().parents[1] / "openapi.yaml"
    output.write_text(json.dumps(app.openapi(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
