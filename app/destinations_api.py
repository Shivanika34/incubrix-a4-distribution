"""Configuration-driven catalogue routes for destinations."""

from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4
from fastapi import APIRouter, Body, Header, Request
from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from sqlalchemy import select

from app.database import Destination, RuleProposal
from app.errors import AppError


class DestinationInput(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    url: HttpUrl
    platform_key: str = Field(min_length=1, max_length=64)
    surface: str = Field(min_length=1, max_length=64)
    connector_key: str = Field(min_length=1, max_length=64)
    category: str | None = None
    audience_tags: list[str] = Field(default_factory=list)
    language: str | None = None
    geo: str | None = None
    est_audience: int | None = Field(default=None, ge=0)
    rules_url: HttpUrl | None = None
    submit_url: HttpUrl | None = None
    frequency_cap: dict = Field(default_factory=dict)
    self_promo_allowed: bool = True
    link_policy: str = Field(default="allow", pattern="^(allow|block|strip)$")
    disclosure_required: bool = False
    automation_allowed: bool = True
    relevance_score: int | None = Field(default=None, ge=1, le=5)
    relevance_reason: str | None = None
    level: str = Field(default="L0", pattern="^L[0-4]$")


class DestinationResponse(DestinationInput):
    model_config = ConfigDict(from_attributes=True)
    id: str
    last_certified_at: datetime | None = None


class RuleProposalInput(BaseModel):
    source_url: HttpUrl
    source_excerpt: str = Field(min_length=1, max_length=5000)
    proposed_rules: dict
    confidence: float = Field(ge=0, le=1)
    warnings: list[str] = Field(default_factory=list)


class RuleApprovalInput(BaseModel):
    confirm: Literal[True]


def add_destination_routes(application) -> None:
    router = APIRouter(prefix="/api/v1/destinations", tags=["destinations"])

    @router.post("", response_model=DestinationResponse, status_code=201)
    def create_destination(body: DestinationInput, request: Request, idempotency_key: str = Header(..., alias="Idempotency-Key")):
        if request.app.state.connector_registry.get(body.connector_key) is None:
            raise AppError("CONNECTOR_NOT_FOUND", f"Connector '{body.connector_key}' is not registered.", False)
        destination = Destination(id=str(uuid4()), **body.model_dump(mode="json"))
        with request.app.state.session_factory.begin() as session:
            session.add(destination)
        return destination

    @router.get("", response_model=list[DestinationResponse])
    def list_destinations(request: Request, platform_key: str | None = None, surface: str | None = None, category: str | None = None, min_relevance: int | None = None, audience_tag: str | None = None):
        with request.app.state.session_factory() as session:
            query = select(Destination)
            if platform_key:
                query = query.where(Destination.platform_key == platform_key)
            if surface:
                query = query.where(Destination.surface == surface)
            if category:
                query = query.where(Destination.category == category)
            if min_relevance is not None:
                query = query.where(Destination.relevance_score >= min_relevance)
            if audience_tag:
                query = query.where(Destination.audience_tags.contains([audience_tag]))
            return list(session.scalars(query.order_by(Destination.name)))

    @router.post("/import", status_code=202)
    async def import_destinations(request: Request, content: bytes = Body(..., media_type="text/csv"), idempotency_key: str = Header(..., alias="Idempotency-Key"), check_reachability: bool = False):
        config = request.app.state.settings
        if len(content) > config.catalogue_import_max_bytes:
            raise AppError("CATALOGUE_IMPORT_TOO_LARGE", "CSV exceeds the configured import size limit.", False, {"max_bytes": config.catalogue_import_max_bytes})
        if check_reachability and not config.catalogue_reachability_enabled:
            raise AppError("REQUEST_VALIDATION_ERROR", "Reachability checks are disabled by configuration.", False)
        try:
            csv_text = content.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise AppError("REQUEST_VALIDATION_ERROR", "CSV must use UTF-8 encoding.", False) from error
        job, _created = request.app.state.jobs.create_catalogue_import(idempotency_key, csv_text, check_reachability=check_reachability)
        from app.jobs_api import _to_response
        return _to_response(job)

    @router.post("/{destination_id}/rule-proposals", status_code=201)
    def propose_rules(destination_id: str, body: RuleProposalInput, request: Request, idempotency_key: str = Header(..., alias="Idempotency-Key")):
        with request.app.state.session_factory.begin() as session:
            if session.get(Destination, destination_id) is None:
                raise AppError("DESTINATION_NOT_FOUND", "Destination was not found.", False)
            proposal = RuleProposal(
                id=str(uuid4()), destination_id=destination_id, source_url=str(body.source_url), source_excerpt=body.source_excerpt,
                proposed_rules=body.proposed_rules, confidence=body.confidence, warnings=body.warnings,
                status="pending", extracted_at=datetime.now(timezone.utc),
            )
            session.add(proposal)
            session.flush()
            return _proposal_dict(proposal)

    @router.get("/{destination_id}/rule-proposals")
    def list_rule_proposals(destination_id: str, request: Request):
        with request.app.state.session_factory() as session:
            if session.get(Destination, destination_id) is None:
                raise AppError("DESTINATION_NOT_FOUND", "Destination was not found.", False)
            proposals = session.scalars(select(RuleProposal).where(RuleProposal.destination_id == destination_id).order_by(RuleProposal.extracted_at.desc()))
            return [_proposal_dict(proposal) for proposal in proposals]

    @router.post("/rule-proposals/{proposal_id}/approve")
    def approve_rule_proposal(proposal_id: str, body: RuleApprovalInput, request: Request, idempotency_key: str = Header(..., alias="Idempotency-Key")):
        allowed_rules = {"frequency_cap", "self_promo_allowed", "link_policy", "disclosure_required", "automation_allowed"}
        with request.app.state.session_factory.begin() as session:
            proposal = session.get(RuleProposal, proposal_id)
            if proposal is None:
                raise AppError("RULE_PROPOSAL_NOT_FOUND", "Rule proposal was not found.", False)
            if proposal.status != "pending":
                raise AppError("REQUEST_VALIDATION_ERROR", "Only pending rule proposals can be approved.", False)
            destination = session.get(Destination, proposal.destination_id)
            unsupported = sorted(set(proposal.proposed_rules) - allowed_rules)
            if unsupported:
                raise AppError("REQUEST_VALIDATION_ERROR", f"Proposal contains unsupported rule fields: {', '.join(unsupported)}.", False)
            for name, value in proposal.proposed_rules.items():
                if name in {"self_promo_allowed", "disclosure_required", "automation_allowed"} and not isinstance(value, bool):
                    raise AppError("REQUEST_VALIDATION_ERROR", f"{name} must be a boolean.", False)
                if name == "link_policy" and value not in {"allow", "block", "strip"}:
                    raise AppError("REQUEST_VALIDATION_ERROR", "link_policy must be allow, block, or strip.", False)
                if name == "frequency_cap":
                    if not isinstance(value, dict) or set(value) - {"max_posts", "window_seconds"} or any(not isinstance(value.get(key), int) or isinstance(value.get(key), bool) or value[key] <= 0 for key in value):
                        raise AppError("REQUEST_VALIDATION_ERROR", "frequency_cap values must be positive integers.", False)
            for name, value in proposal.proposed_rules.items():
                setattr(destination, name, value)
            proposal.status = "approved"
            proposal.approved_at = datetime.now(timezone.utc)
            return _proposal_dict(proposal)

    @router.get("/{destination_id}", response_model=DestinationResponse)
    def get_destination(destination_id: str, request: Request):
        with request.app.state.session_factory() as session:
            destination = session.get(Destination, destination_id)
            if destination is None:
                raise AppError("DESTINATION_NOT_FOUND", "Destination was not found.", False)
            session.expunge(destination)
            return destination

    application.include_router(router)


def _proposal_dict(proposal: RuleProposal) -> dict:
    return {
        "id": proposal.id, "destination_id": proposal.destination_id, "source_url": proposal.source_url, "source_excerpt": proposal.source_excerpt,
        "proposed_rules": proposal.proposed_rules, "confidence": proposal.confidence,
        "warnings": proposal.warnings, "status": proposal.status,
        "extracted_at": proposal.extracted_at.isoformat(),
        "approved_at": proposal.approved_at.isoformat() if proposal.approved_at else None,
    }
