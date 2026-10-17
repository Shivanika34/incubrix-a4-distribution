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
    post_id = f"{platform}-{uuid4().hex[:12]}"
    title = payload.get("title") or payload.get("name") or payload.get("raw", "")
    body = payload.get("body") or payload.get("html") or payload.get("raw", "")
    post = {
        "id": post_id,
        "title": title,
        "body": body,
        "url": f"http://{platform}.local.test/{post_id}",
        "status": "live",
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


@app.api_route("/platform/{path:path}", methods=["GET", "POST", "DELETE"])
async def platform(path: str, request: Request, response: Response):
    if control.delay_seconds:
        await asyncio.sleep(control.delay_seconds)
    response.status_code = control.status_code
    if control.status_code == 429:
        response.headers["X-RateLimit-Reset"] = str(control.reset_seconds)
    if control.status_code != 200:
        return {"path": path, "method": request.method, "status": control.status_code}

    platform_name = _platform_name()
    payload = await request.json() if request.method == "POST" else {}
    if request.method == "POST" and (
        path == "api/v3/post"
        or path == "ghost/api/admin/posts/"
        or path == "posts.json"
        or path == "articles"
    ):
        post = _new_post(payload.get("posts", [{}])[0] if "posts" in payload else payload, platform_name)
        if platform_name == "lemmy":
            return {"post": {"id": post["id"], "ap_id": post["url"]}}
        if platform_name == "ghost":
            return {"posts": [{"id": post["id"], "url": post["url"], "status": "published"}]}
        if platform_name == "devto":
            return {"id": post["id"], "url": post["url"]}
        return {"id": post["id"], "url": post["url"]}

    if request.method == "GET":
        post_id = path.rstrip("/").rsplit("/", 1)[-1].removesuffix(".json")
        post = posts.get(request.query_params.get("id") or post_id)
        if post is None:
            response.status_code = 404
            return {"message": "not found"}
        if platform_name == "lemmy":
            return {"post": {"id": post["id"], "ap_id": post["url"]}}
        if platform_name == "ghost":
            return {"posts": [{"id": post["id"], "url": post["url"], "status": "published"}]}
        if platform_name == "devto":
            return post
        return post

    if request.method == "DELETE" or path == "api/v3/post/delete":
        post_id = str(payload.get("post_id") or path.rstrip("/").rsplit("/", 1)[-1].removesuffix(".json"))
        deleted = posts.pop(post_id, None) is not None
        return {"deleted": deleted, "id": post_id}

    return {"path": path, "method": request.method, "status": control.status_code}
