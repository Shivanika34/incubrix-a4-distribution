"""FastAPI entry point for service health and readiness."""

import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.adapters import ReadinessAdapter, ServiceReadiness
from app.callbacks import CallbackAdapter, HttpCallbackAdapter
from app.config import Settings
from app.database import make_database
from app.errors import AppError
from app.logging_config import configure_logging, correlation_id_var

logger = logging.getLogger(__name__)


def app_error_response(error: AppError) -> JSONResponse:
    status_code = 404 if error.code == "JOB_NOT_FOUND" else 409 if error.code == "IDEMPOTENCY_KEY_REUSED" else 400 if error.code == "IDEMPOTENCY_KEY_REQUIRED" else 503
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": error.code, "message": error.message, "retryable": error.retryable, "details": error.details}},
    )


def create_app(
    settings: Settings | None = None,
    readiness_adapter: ReadinessAdapter | None = None,
    callback_adapter: CallbackAdapter | None = None,
) -> FastAPI:
    config = settings or Settings.from_env()
    configure_logging(config.log_level)
    engine, session_factory = make_database(config.database_url)
    callbacks = callback_adapter or HttpCallbackAdapter(config.callback_hmac_secret)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        if getattr(_app.state, "jobs", None) is not None:
            _app.state.jobs.shutdown()
        engine.dispose()

    application = FastAPI(title="IncuBrix Long-Tail Distribution", version="0.1.0", lifespan=lifespan)
    application.state.session_factory = session_factory
    application.state.readiness = readiness_adapter or ServiceReadiness(session_factory, config)

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

    add_job_routes(application, callbacks, config.echo_sleep_seconds)

    return application


app = create_app()
