"""Controllable local platforms used by simulation and local certification tests."""

import asyncio
import os
from uuid import uuid4
from fastapi import FastAPI, Request, Response
from pydantic import BaseModel, Field

app = FastAPI(title="IncuBrix Local Test Platform")


class MockControl(BaseModel):
    status_code: int = Field(default=200, ge=200, le=599)
    delay_seconds: float = Field(default=0, ge=0, le=30)
    reset_seconds: int = Field(default=1, ge=0, le=3600)


control = MockControl()
posts: dict[str, dict] = {}


def _platform_name() -> str:
    return os.getenv("PLATFORM_KIND", "mock")


def _new_post(payload: dict, platform: str) -> dict:
    post_id = str(uuid4().int % 2_000_000_000) if platform == "lemmy" else f"{platform}-{uuid4().hex[:12]}"
    title = payload.get("title") or payload.get("name") or payload.get("raw", "")
    body = payload.get("body") or payload.get("html") or payload.get("raw", "")
    post = {
        "id": post_id,
        "title": title,
        "body": body,
        "url": f"http://{platform}.local.test/{post_id}",
        "status": "live",
        "updated_at": "2026-01-01T00:00:00.000Z",
    }
    posts[post_id] = post
    return post


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/control")
def set_control(value: MockControl):
    global control
    control = value
    return control


@app.api_route("/platform/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def platform(path: str, request: Request, response: Response):
    if control.delay_seconds:
        await asyncio.sleep(control.delay_seconds)
    response.status_code = control.status_code
    if control.status_code == 429:
        response.headers["X-RateLimit-Reset"] = str(control.reset_seconds)
    if control.status_code != 200:
        return {"path": path, "method": request.method, "status": control.status_code}

    # Simulation connectors route through /platform/{platform} so a single
    # mock service can model all four APIs. Normalize that prefix before
    # matching the platform endpoint, while keeping PLATFORM_KIND for the
    # dedicated local certification fixtures that use /platform directly.
    platform_name = _platform_name()
    route_path = path
    # Only strip the simulation namespace when the remainder is a known API
    # route. Ghost's Admin API itself begins with "ghost/", so its simulation
    # URL contains a doubled "ghost/ghost/" prefix.
    simulation_prefixes = (
        ("ghost", "ghost/ghost/api/admin/posts/", "ghost/"),
        ("devto", "devto/articles", "devto/"),
        ("discourse", "discourse/posts.json", "discourse/"),
        ("lemmy", "lemmy/api/v3/post", "lemmy/"),
    )
    for prefixed_platform, prefix, namespace in simulation_prefixes:
        if path.startswith(prefix):
            platform_name = prefixed_platform
            route_path = path[len(namespace):]
            break
    payload = await request.json() if request.method == "POST" else {}
    if request.method == "POST" and (
        route_path == "api/v3/post"
        or route_path == "ghost/api/admin/posts/"
        or route_path == "posts.json"
        or route_path == "articles"
    ):
        post = _new_post(payload.get("posts", [{}])[0] if "posts" in payload else payload, platform_name)
        if platform_name == "lemmy":
            return {"post": {"id": post["id"], "ap_id": post["url"]}}
        if platform_name == "ghost" or route_path == "ghost/api/admin/posts/":
            return {"posts": [{"id": post["id"], "url": post["url"], "status": "published", "updated_at": post["updated_at"]}]}
        if platform_name == "devto":
            return {"id": post["id"], "url": post["url"], "published": True, "user": {"username": "local-test"}}
        return {"id": post["id"], "url": post["url"]}

    if request.method == "GET":
        post_id = route_path.rstrip("/").rsplit("/", 1)[-1].removesuffix(".json")
        post = posts.get(request.query_params.get("id") or post_id)
        if post is None:
            response.status_code = 404
            return {"message": "not found"}
        if platform_name == "lemmy":
            return {"post": {"id": post["id"], "ap_id": post["url"]}}
        if platform_name == "ghost" or route_path.startswith("ghost/api/admin/posts/"):
            return {"posts": [{"id": post["id"], "url": post["url"], "status": "published", "updated_at": post["updated_at"]}]}
        if platform_name == "devto":
            return post
        return post

    if request.method == "PUT" and route_path.startswith("articles/"):
        post_id = route_path.rstrip("/").rsplit("/", 1)[-1]
        post = posts.get(post_id)
        if post is None:
            response.status_code = 404
            return {"message": "not found"}
        post["published"] = False
        post["status"] = "draft"
        return {**post, "user": {"username": "local-test"}}

    if request.method == "DELETE" or route_path == "api/v3/post/delete":
        post_id = str(payload.get("post_id") or route_path.rstrip("/").rsplit("/", 1)[-1].removesuffix(".json"))
        deleted = posts.pop(post_id, None) is not None
        return {"deleted": deleted, "id": post_id}

    return {"path": path, "method": request.method, "status": control.status_code}
