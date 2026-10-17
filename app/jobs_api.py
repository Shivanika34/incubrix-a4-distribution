"""HTTP routes for submitting and checking background jobs."""

from datetime import datetime

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from app.errors import AppError
from app.jobs import JobService


class EchoRequest(BaseModel):
    text: str = Field(min_length=1, max_length=10000)
    callback_url: HttpUrl | None = None


class PublishingJobRequest(BaseModel):
    content_item: dict = Field(min_length=1)
    destination_ids: list[str] = Field(min_length=1)
    connections: dict[str, dict] = Field(default_factory=dict)


class CertificationRequest(BaseModel):
    destination_id: str = Field(min_length=1)
    content_item: dict | None = None
    connection: dict = Field(default_factory=dict)


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    job_id: str
    status: str
    progress: int
    result_links: list[dict]
    result: dict
    created_at: datetime
    updated_at: datetime


def add_job_routes(application, callback_adapter, echo_sleep_seconds: float, connector_registry=None, disabled_connectors=frozenset()) -> JobService:
    """Attach job endpoints and return their shared in-process worker."""
    jobs = JobService(application.state.session_factory, callback_adapter, echo_sleep_seconds, connector_registry, disabled_connectors)
    application.state.jobs = jobs
    router = APIRouter(prefix="/api/v1/jobs")

    @router.post("/echo", response_model=JobResponse, status_code=202)
    def create_echo(body: EchoRequest, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        if not idempotency_key:
            raise AppError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key header is required.", False)
        job, _created = request.app.state.jobs.create_or_get(
            idempotency_key,
            body.model_dump(mode="json"),
            str(body.callback_url) if body.callback_url else None,
        )
        return _to_response(job)

    @router.get("/{job_id}", response_model=JobResponse)
    def get_job(request: Request, job_id: str):
        return _to_response(request.app.state.jobs.get(job_id))

    publishing_router = APIRouter(prefix="/api/v1/publishing-jobs", tags=["publishing"])

    @publishing_router.post("", response_model=JobResponse, status_code=202)
    def create_publishing_job(
        body: PublishingJobRequest,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        if not idempotency_key:
            raise AppError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key header is required.", False)
        job, _created = request.app.state.jobs.create_publishing(idempotency_key, body.model_dump(mode="json"))
        return _to_response(job)

    certification_router = APIRouter(prefix="/api/v1/certifications", tags=["certifications"])

    @certification_router.post("", response_model=JobResponse, status_code=202)
    def create_certification(
        body: CertificationRequest,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        if not idempotency_key:
            raise AppError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key header is required.", False)
        job, _created = request.app.state.jobs.create_certification(idempotency_key, body.model_dump(mode="json"))
        return _to_response(job)

    application.include_router(router)
    application.include_router(publishing_router)
    application.include_router(certification_router)
    return jobs


def _to_response(job) -> JobResponse:
    return JobResponse(
        job_id=job.id,
        status=job.status,
        progress=job.progress,
        result_links=job.result_links,
        result=job.result,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )
