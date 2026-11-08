"""Authenticated live experiment WebSocket."""

import asyncio

from fastapi import APIRouter, Query, WebSocket
from starlette.websockets import WebSocketState

from app.config import settings
from app.auth import (
    SESSION_COOKIE,
    owned_project,
    project_for_api_key,
    session_identity_from_cookie,
)
from app.state import StateError, live_state

router = APIRouter()


@router.websocket("/api/v1/experiments/{experiment_id}/stream")
async def experiment_stream(
    websocket: WebSocket,
    experiment_id: str,
    project_id: str | None = Query(default=None),
):
    authorization = websocket.headers.get("authorization", "")
    header_key = (
        authorization[7:]
        if authorization.lower().startswith("bearer ")
        else None
    )
    supplied_key = header_key
    if supplied_key:
        project = project_for_api_key(supplied_key)
    else:
        if websocket.headers.get("origin") not in settings.DASHBOARD_ORIGINS:
            await websocket.close(code=4403, reason="Untrusted dashboard origin")
            return
        session = session_identity_from_cookie(
            websocket.cookies.get(SESSION_COOKIE)
        )
        project = (
            owned_project(project_id, session.user.id)
            if project_id and session is not None
            else None
        )
    if project is None:
        await websocket.close(code=4401, reason="Authentication required")
        return

    try:
        subscriber = live_state.subscribe(experiment_id, project.id)
        initial_snapshot = live_state.snapshot(experiment_id, project.id)
    except StateError as exc:
        await websocket.close(
            code=4404 if exc.status_code == 404 else 4429,
            reason=exc.detail,
        )
        return

    receiver = None
    queued_message = None
    last_revision = initial_snapshot["revision"]
    try:
        await websocket.accept()
        await websocket.send_json(initial_snapshot)
        receiver = asyncio.create_task(websocket.receive())
        queued_message = asyncio.create_task(subscriber.queue.get())
        while True:
            done, _ = await asyncio.wait(
                (receiver, queued_message),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if receiver in done:
                frame = receiver.result()
                if frame["type"] == "websocket.disconnect":
                    break
                receiver = asyncio.create_task(websocket.receive())
            if queued_message in done:
                message = queued_message.result()
                if message["type"] == "resync_required":
                    try:
                        message = live_state.snapshot(experiment_id, project.id)
                    except StateError:
                        message = {
                            "type": "experiment_deleted",
                            "experiment_id": experiment_id,
                        }
                if message.get("type") == "snapshot":
                    last_revision = max(last_revision, message["revision"])
                    await websocket.send_json(message)
                elif message.get("revision", last_revision + 1) > last_revision:
                    await websocket.send_json(message)
                    last_revision = message.get("revision", last_revision + 1)
                if message.get("type") in {
                    "experiment_deleted",
                    "experiment_expired",
                }:
                    break
                queued_message = asyncio.create_task(subscriber.queue.get())
    finally:
        tasks = [task for task in (receiver, queued_message) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        live_state.unsubscribe(experiment_id, subscriber.subscriber_id)
        if (
            websocket.client_state is WebSocketState.CONNECTED
            and websocket.application_state is WebSocketState.CONNECTED
        ):
            await websocket.close()
