"""JSON logs with request correlation and optional job context."""

import contextvars
import json
import logging
from datetime import datetime, timezone

correlation_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("correlation_id", default=None)
job_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("job_id", default=None)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname,
                "job_id": getattr(record, "job_id", None) or job_id_var.get(),
                "correlation_id": getattr(record, "correlation_id", None) or correlation_id_var.get(),
                "message": record.getMessage(),
            },
            separators=(",", ":"),
        )


def configure_logging(level: str) -> None:
    root = logging.getLogger()
    if not any(getattr(handler, "_incubrix_json", False) for handler in root.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        handler._incubrix_json = True
        root.addHandler(handler)
    root.setLevel(level)
