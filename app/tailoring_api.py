"""HTTP routes for previewing tailored content and submitting tailoring jobs."""

from __future__ import annotations

from typing import Any
from fastapi import APIRouter, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.jobs_api import JobResponse, _to_response
from app.tailoring import TailoringService


class TailoringPreviewRequest(BaseModel):
    content_item: dict[str, Any] = Field(min_length=1)
    destination_ids: list[str] = Field(min_length=1)
    connections: dict[str, dict[str, Any]] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)
    async_mode: bool = False


class DestinationTailoringResponse(BaseModel):
    destination_id: str
    tailored_content: dict[str, Any]
    applied_transformations: list[dict[str, Any]]
    warnings: list[str]
    rejected_transformations: list[dict[str, Any]]
    validation_results: dict[str, Any]
    validation_errors: list[str]
    original_content_ref: str
    ready_for_publishing: bool


class TailoringPreviewResponse(BaseModel):
    original_content_ref: str
    destinations: list[DestinationTailoringResponse]
    ready_for_publishing: bool


def add_tailoring_routes(application) -> None:
    router = APIRouter(prefix="/api/v1", tags=["tailoring"])

    @router.post(
        "/tailoring/preview",
        response_model=None,
        status_code=200,
        responses={
            200: {"model": TailoringPreviewResponse},
            202: {"model": JobResponse},
        },
    )
    def preview_tailoring(
        body: TailoringPreviewRequest,
        request: Request,
        response: Response,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
    ):
        should_run_async = (
            body.async_mode
            or len(body.destination_ids) > 50
            or request.headers.get("Prefer", "").lower() == "respond-async"
        )
        if should_run_async:
            job, _created = request.app.state.jobs.create_tailoring_preview(
                idempotency_key, body.model_dump(mode="json")
            )
            response.status_code = 202
            return _to_response(job)

        service = TailoringService(request.app.state.session_factory, request.app.state.connector_registry)
        result = service.preview(
            body.content_item,
            body.destination_ids,
            body.connections,
            body.options,
        )
        return TailoringPreviewResponse.model_validate(result)

    @router.post("/tailoring-jobs", response_model=JobResponse, status_code=202)
    def create_tailoring_job(
        body: TailoringPreviewRequest,
        request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
    ):
        job, _created = request.app.state.jobs.create_tailoring_preview(
            idempotency_key, body.model_dump(mode="json")
        )
        return _to_response(job)

    application.include_router(router)
