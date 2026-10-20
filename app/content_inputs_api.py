"""Raw file and guarded URL input routes for content supported by the workflow."""

from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, Field

from app.artifacts import artifact_key, content_digest, parse_content_input
from app.errors import AppError


class URLInputRequest(BaseModel):
    url: str = Field(min_length=1, max_length=4096)


def _store_input(request: Request, data: bytes, content_type: str, filename: str | None, source_url: str | None = None):
    settings = request.app.state.settings
    if len(data) > settings.content_input_max_bytes:
        raise AppError("CONTENT_INPUT_TOO_LARGE", "The input exceeds the configured content size limit.", False, {"max_bytes": settings.content_input_max_bytes})
    content_item = parse_content_input(data, content_type, filename)
    if source_url:
        content_item.setdefault("source_url", source_url)
    digest = content_digest(data)
    artifact = request.app.state.artifact_store.put_bytes(
        artifact_key("inputs", filename, digest=digest), data, content_type
    )
    artifact["sha256"] = digest
    if source_url:
        artifact["source_url"] = source_url
    return {
        "content_item": content_item,
        "input_artifact": artifact,
        "source": {
            "url": source_url,
            "license": content_item.get("license"),
            "rights": content_item.get("rights"),
            "status": "provided" if any(content_item.get(key) for key in ("license", "rights", "source_url")) else "unknown",
        },
    }


def add_content_input_routes(application) -> None:
    router = APIRouter(prefix="/api/v1/content-inputs", tags=["content inputs"])

    @router.post(
        "/upload",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {"schema": {"type": "string", "format": "binary"}},
                    "text/plain": {"schema": {"type": "string", "format": "binary"}},
                    "text/markdown": {"schema": {"type": "string", "format": "binary"}},
                },
            }
        },
    )
    async def upload_content_file(
        request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        filename: str | None = None,
    ):
        del idempotency_key  # Input creation is repeatable; the key is required for POST contract consistency.
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        chunks = bytearray()
        async for chunk in request.stream():
            chunks.extend(chunk)
            if len(chunks) > request.app.state.settings.content_input_max_bytes:
                raise AppError("CONTENT_INPUT_TOO_LARGE", "The input exceeds the configured content size limit.", False, {"max_bytes": request.app.state.settings.content_input_max_bytes})
        return _store_input(request, bytes(chunks), content_type, filename)

    @router.post("/from-url")
    def import_content_url(
        body: URLInputRequest,
        request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
    ):
        del idempotency_key
        content, content_type, final_url = request.app.state.content_fetcher.fetch(
            body.url, request.app.state.settings.content_input_max_bytes
        )
        filename = Path(urlsplit(final_url).path).name or None
        return _store_input(request, content, content_type, filename, final_url)

    application.include_router(router)
