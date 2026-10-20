"""Read-only routes for browsing the configured public Discourse community."""

from fastapi import APIRouter, Path, Query, Request

from app.discourse_community import DiscourseCommunityClient


def categories(request: Request):
    client: DiscourseCommunityClient = request.app.state.discourse_community
    return {"community_url": client.public_base_url, "categories": client.categories()}


def topics(request: Request, per_page: int = Query(default=20, ge=1, le=50)):
    client: DiscourseCommunityClient = request.app.state.discourse_community
    return {"community_url": client.public_base_url, "topics": client.topics(per_page)}


def topic(request: Request, topic_id: int = Path(ge=1)):
    client: DiscourseCommunityClient = request.app.state.discourse_community
    result = client.topic(topic_id)
    result["url"] = f"{client.public_base_url}{result['url']}"
    return result


def add_discourse_community_routes(application) -> None:
    router = APIRouter(prefix="/api/v1/community/discourse", tags=["discourse-community"])
    router.add_api_route("/categories", categories, methods=["GET"])
    router.add_api_route("/topics", topics, methods=["GET"])
    router.add_api_route("/topics/{topic_id}", topic, methods=["GET"])
    application.include_router(router)
