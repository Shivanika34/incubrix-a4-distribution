"""Configuration-driven catalogue routes for destinations."""

from datetime import datetime
from uuid import uuid4
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from sqlalchemy import select

from app.database import Destination
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


def add_destination_routes(application) -> None:
    router = APIRouter(prefix="/api/v1/destinations", tags=["destinations"])

    @router.post("", response_model=DestinationResponse, status_code=201)
    def create_destination(body: DestinationInput, request: Request):
        if request.app.state.connector_registry.get(body.connector_key) is None:
            raise AppError("CONNECTOR_NOT_FOUND", f"Connector '{body.connector_key}' is not registered.", False)
        destination = Destination(id=str(uuid4()), **body.model_dump(mode="json"))
        with request.app.state.session_factory.begin() as session:
            session.add(destination)
        return destination

    @router.get("", response_model=list[DestinationResponse])
    def list_destinations(request: Request):
        with request.app.state.session_factory() as session:
            return list(session.scalars(select(Destination).order_by(Destination.name)))

    @router.get("/{destination_id}", response_model=DestinationResponse)
    def get_destination(destination_id: str, request: Request):
        with request.app.state.session_factory() as session:
            destination = session.get(Destination, destination_id)
            if destination is None:
                raise AppError("DESTINATION_NOT_FOUND", "Destination was not found.", False)
            session.expunge(destination)
            return destination

    application.include_router(router)
