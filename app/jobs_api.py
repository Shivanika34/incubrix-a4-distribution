"""HTTP routes for submitting and checking background jobs."""

from datetime import datetime

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from app.errors import AppError
from app.jobs import JobService


class EchoRequest(BaseModel):
    text: str = Field(min_length=1, max_length=10000)
    callback_url: HttpUrl | None = None


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    job_id: str
    status: str
    progress: int
    result_links: list[dict]
    created_at: datetime
    updated_at: datetime


def add_job_routes(application, callback_adapter, echo_sleep_seconds: float) -> JobService:
    """Attach job endpoints and return their shared in-process worker."""
    jobs = JobService(application.state.session_factory, callback_adapter, echo_sleep_seconds)
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

    application.include_router(router)
    return jobs


def _to_response(job) -> JobResponse:
    return JobResponse(
        job_id=job.id,
        status=job.status,
        progress=job.progress,
        result_links=job.result_links,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )
