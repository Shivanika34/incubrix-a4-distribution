"""HTTP routes for destination recommendation."""

from __future__ import annotations

from typing import Any
from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, Field

from app.recommender import RecommendationService


class RecommendationRequest(BaseModel):
    content_item: dict[str, Any] = Field(min_length=1)
    topic: str | None = None
    audience_tags: list[str] = Field(default_factory=list)
    candidate_destination_ids: list[str] | None = None
    creator_preferences: dict[str, Any] = Field(default_factory=dict)
    top_k: int = Field(default=10, ge=1, le=100)
    min_score: float = Field(default=0.0, ge=0.0, le=100.0)
    include_excluded: bool = False


class RecommendationItem(BaseModel):
    destination_id: str
    destination_name: str
    score: float
    rank: int
    matched_categories: list[str]
    matched_tags: list[str]
    explanation: str
    positive_signals: list[str]
    negative_signals: list[str]
    eligibility: str
    rule_eligibility: dict[str, Any]
    readiness: dict[str, Any]
    warnings: list[str]


class ExclusionItem(BaseModel):
    destination_id: str
    destination_name: str
    score: float
    reason: str
    warnings: list[str]


class RecommendationResponse(BaseModel):
    total_candidates: int
    total_evaluated: int
    recommendations: list[RecommendationItem]
    exclusions: list[ExclusionItem]


def add_recommender_routes(application) -> None:
    router = APIRouter(prefix="/api/v1/recommendations", tags=["recommendations"])

    @router.post("", response_model=RecommendationResponse, status_code=200)
    def get_recommendations(
        body: RecommendationRequest,
        request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
    ):
        service = RecommendationService(
            request.app.state.session_factory,
            request.app.state.connector_registry,
            request.app.state.disabled_connectors,
        )
        result = service.recommend(
            body.content_item,
            topic=body.topic,
            audience_tags=body.audience_tags,
            candidate_destination_ids=body.candidate_destination_ids,
            creator_preferences=body.creator_preferences,
            top_k=body.top_k,
            min_score=body.min_score,
            include_excluded=body.include_excluded,
        )
        return RecommendationResponse.model_validate(result)

    application.include_router(router)
