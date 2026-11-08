"""Backend application entry point."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routes import events, experiments, metrics, projects
from app.routes import authentication
from app.routes.stream import router as stream_router
from app.state import live_state


class RequestSizeLimit:
    def __init__(self, app, maximum_bytes: int):
        self.app = app
        self.maximum_bytes = maximum_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                body_size = int(content_length)
            except ValueError:
                await self._reject(send, 400, "Invalid Content-Length")
                return
            if body_size < 0:
                await self._reject(send, 400, "Invalid Content-Length")
                return
            if body_size > self.maximum_bytes:
                await self._reject(send, 413, "Request body too large")
                return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.maximum_bytes:
                await self._reject(send, 413, "Request body too large")
                return
            if not message.get("more_body", False):
                break

        replayed = False

        async def receive_with_body():
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {
                "type": "http.request",
                "body": bytes(body),
                "more_body": False,
            }

        await self.app(scope, receive_with_body, send)

    @staticmethod
    async def _reject(send, status_code: int, detail: str):
        body = ('{"detail":"' + detail + '"}').encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


@asynccontextmanager
async def lifespan(_app: FastAPI):
    async def expire_idle_experiments():
        while True:
            live_state.expire_idle()
            await asyncio.sleep(min(60, max(1, settings.EXPERIMENT_IDLE_SECONDS)))

    expiration_task = asyncio.create_task(expire_idle_experiments())
    try:
        yield
    finally:
        expiration_task.cancel()
        await asyncio.gather(expiration_task, return_exceptions=True)


app = FastAPI(
    title="NeuronScope API",
    version="1.0.0",
    description="Real-time, process-local neural network monitoring",
    lifespan=lifespan,
)

app.add_middleware(RequestSizeLimit, maximum_bytes=settings.MAX_REQUEST_BYTES)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(set(settings.CORS_ORIGINS + settings.DASHBOARD_ORIGINS)),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(projects.router)
app.include_router(authentication.router)
app.include_router(experiments.router)
app.include_router(metrics.router)
app.include_router(events.router)
app.include_router(stream_router)


@app.get("/")
def home():
    return {
        "service": "NeuronScope",
        "status": "running"
    }


@app.get("/health")
def health():
    from sqlalchemy import text
    from app.database import engine

    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))

    return {"status": "healthy", "database": "connected"}
