"""FastAPI entry point for service health and readiness."""

import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.adapters import DatabaseReadiness, ReadinessAdapter, ServiceReadiness
from app.artifacts import EphemeralArtifactStore, S3ArtifactStore, SafeURLFetcher
from app.callbacks import CallbackAdapter, HttpCallbackAdapter
from app.config import Settings
from app.database import initialize_database_schema, make_database
from app.connectors.registry import ConnectorRegistry
from app.errors import AppError
from app.discourse_community import DiscourseCommunityClient
from app.logging_config import configure_logging, correlation_id_var

logger = logging.getLogger(__name__)


def app_error_response(error: AppError) -> JSONResponse:
    status_code = 404 if error.code in {"JOB_NOT_FOUND", "DESTINATION_NOT_FOUND", "CONNECTOR_NOT_FOUND", "RULE_PROPOSAL_NOT_FOUND", "DISCOURSE_TOPIC_NOT_FOUND", "ARTIFACT_NOT_FOUND"} else 429 if error.code == "PLATFORM_RATE_LIMIT" else 502 if error.code == "DISCOURSE_UPSTREAM_ERROR" else 413 if error.code in {"CATALOGUE_IMPORT_TOO_LARGE", "CONTENT_INPUT_TOO_LARGE"} else 409 if error.code == "IDEMPOTENCY_KEY_REUSED" else 422 if error.code in {"DEST_SPEC_VIOLATION", "CERTIFICATION_FAILED", "REQUEST_VALIDATION_ERROR", "CONTENT_INPUT_INVALID", "CONTENT_INPUT_TYPE_UNSUPPORTED", "CONTENT_URL_UNSAFE", "CONTENT_URL_REDIRECT_LIMIT"} else 403 if error.code == "CONNECTOR_DISABLED" else 400 if error.code in {"IDEMPOTENCY_KEY_REQUIRED", "CONNECTOR_METHOD_NOT_FOUND"} else 503
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": error.code, "message": error.message, "retryable": error.retryable, "details": error.details}},
    )


def create_app(
    settings: Settings | None = None,
    readiness_adapter: ReadinessAdapter | None = None,
    callback_adapter: CallbackAdapter | None = None,
    artifact_store=None,
    content_fetcher=None,
    discourse_community_client=None,
) -> FastAPI:
    config = settings or Settings.from_env()
    configure_logging(config.log_level)
    engine, session_factory = make_database(config.database_url)
    callbacks = callback_adapter or HttpCallbackAdapter(config.callback_hmac_secret)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            initialize_database_schema(engine)
            yield
        finally:
            if getattr(_app.state, "jobs", None) is not None:
                _app.state.jobs.shutdown()
            engine.dispose()

    application = FastAPI(title="IncuBrix Long-Tail Distribution", version="0.1.0", lifespan=lifespan)
    application.state.session_factory = session_factory
    application.state.settings = config
    application.state.artifact_store = artifact_store or (
        EphemeralArtifactStore(
            os.getenv("DEMO_ARTIFACT_DIRECTORY", "/tmp/incubrix-artifacts"),
            os.getenv("RENDER_EXTERNAL_URL", ""),
        )
        if os.getenv("DEMO_EPHEMERAL_ARTIFACTS", "false").lower() == "true"
        else S3ArtifactStore(
            config.minio_endpoint,
            config.minio_access_key,
            config.minio_secret_key,
            config.minio_bucket,
            config.minio_public_endpoint,
        )
    )
    application.state.content_fetcher = content_fetcher or SafeURLFetcher(
        timeout_seconds=config.content_url_timeout_seconds
    )
    application.state.discourse_community = discourse_community_client or DiscourseCommunityClient(
        config.discourse_base_url,
        public_base_url=config.discourse_public_url,
        host_header=config.discourse_host_header,
    )
    application.state.settings = config
    application.state.readiness = readiness_adapter or (
        DatabaseReadiness(session_factory)
        if isinstance(application.state.artifact_store, EphemeralArtifactStore)
        else ServiceReadiness(session_factory, config)
    )
    application.state.connector_registry = ConnectorRegistry(config.connectors_dir, config)
    application.state.disabled_connectors = config.connector_disabled

    @application.middleware("http")
    async def add_correlation_id(request: Request, call_next):
        supplied = request.headers.get("X-Correlation-Id", "").strip()
        correlation_id = supplied or str(uuid.uuid4())
        token = correlation_id_var.set(correlation_id)
        try:
            response = await call_next(request)
            response.headers["X-Correlation-Id"] = correlation_id
            logger.info("request completed", extra={"correlation_id": correlation_id})
            return response
        finally:
            correlation_id_var.reset(token)

    @application.exception_handler(AppError)
    async def app_error_handler(_request: Request, error: AppError):
        return app_error_response(error)

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(_request: Request, error: RequestValidationError):
        return JSONResponse(status_code=422, content={"error": {"code": "REQUEST_VALIDATION_ERROR", "message": "The request body is invalid.", "retryable": False, "details": jsonable_encoder(error.errors())}})

    @application.exception_handler(StarletteHTTPException)
    async def http_error_handler(_request: Request, error: StarletteHTTPException):
        return JSONResponse(status_code=error.status_code, content={"error": {"code": "HTTP_ERROR", "message": str(error.detail), "retryable": error.status_code >= 500, "details": None}})

    @application.exception_handler(Exception)
    async def unexpected_error_handler(_request: Request, _error: Exception):
        logger.exception("unhandled application error")
        return JSONResponse(status_code=500, content={"error": {"code": "INTERNAL_ERROR", "message": "An unexpected error occurred.", "retryable": True, "details": None}})

    @application.get("/health")
    def health():
        return {"status": "ok"}

    @application.get("/ready")
    def ready(request: Request, response: Response):
        try:
            request.app.state.readiness.check()
            return {"status": "ready"}
        except Exception:
            response.status_code = 503
            return {"status": "not_ready"}

    from app.jobs_api import add_job_routes
    from app.destinations_api import add_destination_routes
    from app.connectors_api import add_connector_routes
    from app.tailoring_api import add_tailoring_routes
    from app.recommender_api import add_recommender_routes
    from app.orchestrator_api import add_orchestrator_routes
    from app.content_inputs_api import add_content_input_routes
    from app.discourse_community_api import add_discourse_community_routes

    add_job_routes(
        application,
        callbacks,
        config.echo_sleep_seconds,
        application.state.connector_registry,
        application.state.disabled_connectors,
        simulation_mode=config.simulation_mode,
        evidence_dir=config.certification_evidence_dir,
        catalogue_import_max_rows=config.catalogue_import_max_rows,
        catalogue_reachability_enabled=config.catalogue_reachability_enabled,
        artifact_store=application.state.artifact_store,
    )
    add_destination_routes(application)
    add_connector_routes(application)
    add_tailoring_routes(application)
    add_recommender_routes(application)
    add_orchestrator_routes(application)
    add_content_input_routes(application)
    add_discourse_community_routes(application)

    if isinstance(application.state.artifact_store, EphemeralArtifactStore):
        @application.get("/api/v1/demo-artifacts/{key:path}", include_in_schema=False)
        def get_demo_artifact(key: str):
            data, content_type = application.state.artifact_store.get_bytes(key)
            return Response(content=data, media_type=content_type)

    @application.get("/", include_in_schema=False)
    def publishing_demo():
        return FileResponse(Path(__file__).resolve().parents[1] / "web" / "index.html", media_type="text/html")

    return application


app = create_app()
