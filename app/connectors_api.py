"""HTTP adapter for Appendix A connector methods and rule enforcement."""

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.database import RuleDecision
from app.errors import AppError
from app.rules import check_rules


class ConnectorRequest(BaseModel):
    rendition: dict = Field(default_factory=dict)
    destination: dict = Field(default_factory=dict)
    connection: dict = Field(default_factory=dict)
    idempotency_key: str | None = None
    platform_post_id: str | None = None
    since: str | None = None
    post_history: list[dict | str] = Field(default_factory=list)


def add_connector_routes(application) -> None:
    router = APIRouter(prefix="/connector/v1", tags=["connectors"])

    @router.post("/{connector_key}/{method}")
    def dispatch(connector_key: str, method: str, body: ConnectorRequest, request: Request):
        if connector_key in request.app.state.disabled_connectors:
            raise AppError("CONNECTOR_DISABLED", f"Connector '{connector_key}' is disabled by configuration.", False)
        connector = request.app.state.connector_registry.get(connector_key)
        if connector is None:
            raise AppError("CONNECTOR_NOT_FOUND", f"Connector '{connector_key}' is not registered.", False)
        if not hasattr(connector, method) or method.startswith("_"):
            raise AppError("CONNECTOR_METHOD_NOT_FOUND", f"Connector method '{method}' is not available.", False)

        if method == "publish":
            decision = check_rules(body.rendition, body.destination, body.post_history)
            with request.app.state.session_factory.begin() as session:
                session.add(RuleDecision(destination_id=body.destination.get("id"), outcome=decision.outcome, reason=decision.reason, error_code=decision.error_code))
            if decision.outcome != "allowed":
                raise AppError(decision.error_code or "DEST_SPEC_VIOLATION", decision.reason, decision.outcome == "reschedule", {"retry_at": decision.retry_at} if decision.retry_at else None)
            validation = connector.validate(decision.rendition or body.rendition, body.destination, body.connection)
            if not validation["ok"]:
                raise AppError("DEST_SPEC_VIOLATION", "The rendition violates the destination specification.", False, validation["violations"])
            return connector.publish(decision.rendition or body.rendition, body.destination, body.connection, body.idempotency_key)
        if method == "validate":
            return connector.validate(body.rendition, body.destination, body.connection)
        if method in {"get_status", "delete", "fetch_metrics"}:
            if not body.platform_post_id:
                raise AppError("REQUEST_VALIDATION_ERROR", "platform_post_id is required for this method.", False)
            function = getattr(connector, method)
            return function(body.platform_post_id, body.connection) if method != "fetch_metrics" else function(body.platform_post_id, body.connection, body.since)
        if method == "refresh_auth":
            return connector.refresh_auth(body.connection)
        if method == "build_assist_pack":
            return connector.build_assist_pack(body.rendition, body.destination)
        raise AppError("CONNECTOR_METHOD_NOT_FOUND", f"Connector method '{method}' is not available.", False)

    application.include_router(router)
