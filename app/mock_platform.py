"""A controllable local platform used by connector tests and simulation-mode demos."""

import asyncio
from fastapi import FastAPI, Request, Response
from pydantic import BaseModel, Field

app = FastAPI(title="IncuBrix Mock Platform")


class MockControl(BaseModel):
    status_code: int = Field(default=200, ge=200, le=599)
    delay_seconds: float = Field(default=0, ge=0, le=30)
    reset_seconds: int = Field(default=1, ge=0, le=3600)


control = MockControl()


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
    return {"path": path, "method": request.method, "status": control.status_code}
