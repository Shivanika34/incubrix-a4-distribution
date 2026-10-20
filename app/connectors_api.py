"""HTTP adapter for Appendix A connector methods and rule enforcement."""

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, Field
import os

from app.database import Destination, RuleDecision
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


class ConnectorConfigurationCheck(BaseModel):
    destination_id: str = Field(min_length=1)
    connection: dict = Field(default_factory=dict)


_WORKFLOW_CONNECTORS = {
    "ghost": {
        "label": "Ghost",
        "required": ["admin_api_key"],
    },
    "devto": {
        "label": "DEV.to",
        "required": ["api_key"],
    },
    "discourse": {
        "label": "Discourse",
        "required": ["api_key", "api_username"],
    },
    "lemmy": {
        "label": "Lemmy",
        "required_any": [["auth"], ["username", "password"]],
    },
}


def server_connection(connector_key: str, destination: dict, supplied: dict | None = None) -> dict:
    """Merge configured server credentials into a request without exposing them."""
    connection = dict(supplied or {})
    env_names = {
        "ghost": {"admin_api_key": "GHOST_REAL_ADMIN_API_KEY"},
        "devto": {"api_key": "DEVTO_REAL_API_KEY", "username": "DEVTO_REAL_USERNAME"},
        "discourse": {"api_key": "DISCOURSE_REAL_API_KEY", "api_username": "DISCOURSE_REAL_API_USERNAME"},
        "lemmy": {"username": "LEMMY_REAL_USERNAME", "password": "LEMMY_REAL_PASSWORD"},
    }
    for field, env_name in env_names.get(connector_key, {}).items():
        value = os.getenv(env_name, "").strip()
        if value:
            connection[field] = value
    base_env = {"ghost": "GHOST_REAL_BASE_URL", "devto": "DEVTO_REAL_BASE_URL", "discourse": "DISCOURSE_REAL_BASE_URL", "lemmy": "LEMMY_REAL_BASE_URL"}.get(connector_key)
    base_url = os.getenv(base_env, "").strip() if base_env else ""
    if base_url and not connection.get("base_url"):
        connection["base_url"] = base_url
    if connector_key == "lemmy":
        community = os.getenv("LEMMY_REAL_COMMUNITY_ID", "").strip()
        if community and not connection.get("community_id"):
            connection["community_id"] = community
    if connector_key == "discourse":
        host_header = os.getenv("DISCOURSE_HOST_HEADER", "").strip()
        if host_header and not connection.get("host_header"):
            connection["host_header"] = host_header
    return connection


def _server_configured(connector_key: str) -> bool:
    fields = {
        "ghost": ("GHOST_REAL_ADMIN_API_KEY",),
        "devto": ("DEVTO_REAL_API_KEY",),
        "discourse": ("DISCOURSE_REAL_API_KEY", "DISCOURSE_REAL_API_USERNAME"),
        "lemmy": ("LEMMY_REAL_USERNAME", "LEMMY_REAL_PASSWORD"),
    }.get(connector_key, ())
    return bool(fields) and all(os.getenv(name, "").strip() for name in fields)


def add_connector_routes(application) -> None:
    api_router = APIRouter(prefix="/api/v1/connectors", tags=["connectors"])

    @api_router.get("")
    def list_workflow_connectors(request: Request):
        registry = request.app.state.connector_registry
        entries = []
        for key, ui_info in _WORKFLOW_CONNECTORS.items():
            manifest = registry.manifests.get(key)
            entries.append({
                "key": key,
                "label": ui_info["label"],
                "registered": manifest is not None and registry.get(key) is not None,
                "enabled": key not in request.app.state.disabled_connectors,
                "version": manifest.get("version") if manifest else None,
                "api_version": manifest.get("api_version") if manifest else None,
                "capabilities": manifest.get("capabilities", []) if manifest else [],
                "surfaces": manifest.get("surfaces", []) if manifest else [],
                "server_configured": _server_configured(key),
            })
        return {
            "simulation_mode": request.app.state.settings.simulation_mode,
            "connectors": entries,
        }

    @api_router.post("/{connector_key}/configuration-check")
    def check_connector_configuration(
        connector_key: str,
        body: ConnectorConfigurationCheck,
        request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
    ):
        # This is a local shape/required-field check only. It deliberately makes
        # no request to the destination and never returns connection values.
        del idempotency_key
        if connector_key not in _WORKFLOW_CONNECTORS:
            raise AppError("CONNECTOR_NOT_FOUND", "Connector is not available in the publishing workflow.", False)
        with request.app.state.session_factory() as session:
            destination = session.get(Destination, body.destination_id)
            if destination is None:
                raise AppError("DESTINATION_NOT_FOUND", "Destination was not found.", False)
            if destination.connector_key != connector_key:
                raise AppError("REQUEST_VALIDATION_ERROR", "Destination connector does not match the requested connector.", False)
            destination_data = {column.name: getattr(destination, column.name) for column in Destination.__table__.columns}

        info = _WORKFLOW_CONNECTORS[connector_key]
        connection = server_connection(connector_key, destination_data, body.connection)
        missing = [
            name for name in info.get("required", [])
            if not isinstance(connection.get(name), str) or not connection[name].strip()
        ]
        alternatives = info.get("required_any", [])
        if alternatives and not any(
            all(isinstance(connection.get(name), str) and connection[name].strip() for name in group)
            for group in alternatives
        ):
            missing.append(" or ".join(" + ".join(group) for group in alternatives))
        invalid = []
        if connector_key == "ghost" and "admin_api_key" not in missing:
            try:
                key_id, secret = connection["admin_api_key"].split(":", 1)
                if not key_id or not secret or len(bytes.fromhex(secret)) == 0:
                    raise ValueError
            except (ValueError, TypeError):
                invalid.append("admin_api_key (expected id:hex-secret)")

        lemmy_connector = request.app.state.connector_registry.get("lemmy") if connector_key == "lemmy" else None
        local_test_requested = bool(lemmy_connector and lemmy_connector._local_test_requested(connection))
        local_test_kind = None
        if local_test_requested:
            local_connection = {
                **connection,
                "destination_id": destination_data["id"],
                "destination_url": destination_data["url"],
                "connector_key": destination_data["connector_key"],
                "surface": destination_data["surface"],
                "community_id": connection.get("community_id") or destination_data.get("category"),
                "base_url": connection.get("base_url") or destination_data["url"],
            }
            local_test_kind = lemmy_connector.local_test_kind(destination_data, local_connection)
            if local_test_kind is None:
                invalid.append("local_test (destination, internal URL, and community must match the configured local target)")
            elif local_test_kind == "local_mock_fixture":
                missing = [field for field in missing if field != "auth or username + password"]

        registered = request.app.state.connector_registry.get(connector_key) is not None
        enabled = connector_key not in request.app.state.disabled_connectors
        if not registered or not enabled:
            status = "unavailable"
            message = "Connector is not registered or is disabled by application configuration."
        elif invalid:
            status = "invalid_configuration"
            message = "One or more connection fields have an invalid format."
        elif local_test_requested and missing:
            status = "missing_configuration"
            message = "The approved local Lemmy instance requires an auth token or username and password."
        elif local_test_kind == "local_real_instance":
            status = "local_real_instance"
            message = "The approved local Lemmy target and credential fields are configured; remote authentication has not been tested."
        elif local_test_kind == "local_mock_fixture":
            status = "local_mock_fixture"
            message = "The registered Lemmy mock fixture is selected; no real platform authentication was tested."
        elif request.app.state.settings.simulation_mode and missing:
            status = "simulation_mode"
            message = "Simulation mode uses the configured mock platform; real account credentials are not required or tested."
        elif missing:
            status = "missing_configuration"
            message = "Required connection fields are missing or invalid."
        else:
            status = "configuration_valid"
            message = "Required fields are present. Remote authentication has not been tested."
        orchestration_ready = (
            destination_data.get("level") in {"L3", "L4"}
            and destination_data.get("last_certified_at") is not None
            and destination_data.get("automation_allowed", True)
        )
        return {
            "connector_key": connector_key,
            "destination_id": body.destination_id,
            "status": status,
            "configuration_valid": status in {"configuration_valid", "local_real_instance", "local_mock_fixture"},
            "ready_for_dispatch": status in {"configuration_valid", "simulation_mode", "local_real_instance", "local_mock_fixture"},
            "missing_fields": missing + invalid,
            "remote_verified": False,
            "simulation_mode": request.app.state.settings.simulation_mode,
            "execution_target": (
                local_test_kind
                if local_test_kind is not None
                else "mock_simulation" if request.app.state.settings.simulation_mode and not local_test_requested
                else "configured_destination"
            ),
            "orchestrator_ready": orchestration_ready,
            "orchestrator_readiness_reason": None if orchestration_ready else "Automated orchestration requires automation permission and verified L3/L4 certification.",
            "message": message,
        }

    application.include_router(api_router)

    router = APIRouter(prefix="/connector/v1", tags=["connectors"])

    @router.post("/{connector_key}/{method}")
    def dispatch(connector_key: str, method: str, body: ConnectorRequest, request: Request, idempotency_key: str = Header(..., alias="Idempotency-Key")):
        if connector_key in request.app.state.disabled_connectors:
            raise AppError("CONNECTOR_DISABLED", f"Connector '{connector_key}' is disabled by configuration.", False)
        connector = request.app.state.connector_registry.get(connector_key)
        if connector is None:
            raise AppError("CONNECTOR_NOT_FOUND", f"Connector '{connector_key}' is not registered.", False)
        capabilities = request.app.state.connector_registry.manifests[connector_key]["capabilities"]
        method_capability = {"publish": "publish", "get_status": "status", "delete": "delete", "fetch_metrics": "metrics", "build_assist_pack": "assist"}
        if method in method_capability and method_capability[method] not in capabilities:
            raise AppError("CONNECTOR_METHOD_NOT_FOUND", f"Connector method '{method}' is not available.", False)
        if not hasattr(connector, method) or method.startswith("_"):
            raise AppError("CONNECTOR_METHOD_NOT_FOUND", f"Connector method '{method}' is not available.", False)

        local_test_requested = connector_key == "lemmy" and connector._local_test_requested(body.connection)
        if local_test_requested and method in {"publish", "get_status", "delete"}:
            destination_id = body.destination.get("id") if method == "publish" else body.connection.get("destination_id")
            if not destination_id:
                raise AppError("REQUEST_VALIDATION_ERROR", "A registered destination ID is required for the local Lemmy override.", False)
            with request.app.state.session_factory() as session:
                destination_record = session.get(Destination, str(destination_id))
                if destination_record is None or destination_record.connector_key != "lemmy":
                    raise AppError("REQUEST_VALIDATION_ERROR", "The local Lemmy override is restricted to its registered destination.", False)
                destination_data = {column.name: getattr(destination_record, column.name) for column in Destination.__table__.columns}
            local_connection = {
                **body.connection,
                "destination_id": destination_data["id"],
                "destination_url": destination_data["url"],
                "connector_key": destination_data["connector_key"],
                "surface": destination_data["surface"],
                "community_id": body.connection.get("community_id") or destination_data.get("category"),
                "base_url": body.connection.get("base_url") or destination_data["url"],
            }
            if not connector.is_approved_local_test(destination_data, local_connection):
                raise AppError("REQUEST_VALIDATION_ERROR", "The local Lemmy override is restricted to its configured destination and internal URL.", False)
            body.connection = local_connection
            if method in {"publish", "get_status", "delete"}:
                body.destination = destination_data

        if method == "publish":
            body.idempotency_key = idempotency_key
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
            if method == "get_status" and connector_key == "lemmy" and local_test_requested:
                return function(body.platform_post_id, body.connection, body.destination)
            return function(body.platform_post_id, body.connection) if method != "fetch_metrics" else function(body.platform_post_id, body.connection, body.since)
        if method == "refresh_auth":
            return connector.refresh_auth(body.connection)
        if method == "build_assist_pack":
            return connector.build_assist_pack(body.rendition, body.destination)
        raise AppError("CONNECTOR_METHOD_NOT_FOUND", f"Connector method '{method}' is not available.", False)

    application.include_router(router)
