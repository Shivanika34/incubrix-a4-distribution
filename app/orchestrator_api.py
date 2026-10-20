"""HTTP routes for publishing orchestration."""

from __future__ import annotations

from time import perf_counter
from typing import Any
from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, Field, model_validator

from app.jobs_api import JobResponse, _to_response
from app.artifacts import store_orchestration_artifacts
from app.orchestrator import PublishingOrchestrator


class RecommendationSettings(BaseModel):
    topic: str | None = None
    audience_tags: list[str] = Field(default_factory=list)
    candidate_destination_ids: list[str] | None = None
    creator_preferences: dict[str, Any] = Field(default_factory=dict)
    top_k: int = Field(default=5, ge=1, le=100)
    min_score: float = Field(default=0.0, ge=0.0, le=100.0)


class OrchestrationRequest(BaseModel):
    content_item: dict[str, Any] = Field(min_length=1)
    destination_ids: list[str] | None = None
    recommendation: RecommendationSettings | None = None
    dispatch: bool = False
    connections: dict[str, dict[str, Any]] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)
    input_artifacts: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_selection(self):
        if self.destination_ids is not None and not self.destination_ids:
            raise ValueError("destination_ids must contain at least one destination when provided")
        if self.destination_ids is not None and self.recommendation is not None:
            raise ValueError("provide destination_ids or recommendation, not both")
        if self.destination_ids is None and self.recommendation is None:
            # Default recommendation settings keep the convenient recommend-all workflow explicit.
            self.recommendation = RecommendationSettings()
        return self


def add_orchestrator_routes(application) -> None:
    router = APIRouter(prefix="/api/v1/publishing-orchestrations", tags=["orchestration"])

    @router.post(
        "",
        response_model=JobResponse,
        status_code=202,
    )
    def create_publishing_orchestration(
        body: OrchestrationRequest,
        request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
    ):
        job, _created = request.app.state.jobs.create_orchestration(
            idempotency_key,
            body.model_dump(mode="json"),
        )
        return _to_response(job)

    @router.post(
        "/preview",
        status_code=200,
    )
    def preview_publishing_orchestration(
        body: OrchestrationRequest,
        request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
    ):
        orchestrator = PublishingOrchestrator(
            request.app.state.session_factory,
            request.app.state.connector_registry,
            request.app.state.disabled_connectors,
            simulation_mode=request.app.state.settings.simulation_mode,
        )
        payload = body.model_dump(mode="json")
        payload["dispatch"] = False
        started = perf_counter()
        result = orchestrator.run_workflow(payload, idempotency_key)
        artifact_refs = store_orchestration_artifacts(
            request.app.state.artifact_store,
            job_id=f"preview-{idempotency_key}",
            request_body=payload,
            report=result,
            settings={
                "simulation_mode": request.app.state.settings.simulation_mode,
                "disabled_connectors": sorted(request.app.state.disabled_connectors),
            },
            connector_names=[],
            duration_ms=round((perf_counter() - started) * 1000),
        )
        result["artifacts"] = artifact_refs
        return result

    application.include_router(router)
