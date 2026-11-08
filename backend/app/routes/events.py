"""Collapse event ingestion and retrieval."""

from fastapi import APIRouter, Depends, HTTPException

from app.auth import ProjectIdentity, require_project
from app.schemas import CollapseEventCreate
from app.state import StateError, live_state

router = APIRouter(prefix="/api/v1/experiments", tags=["Collapse Events"])


@router.post("/{experiment_id}/events")
def register_collapse_event(
    experiment_id: str,
    body: CollapseEventCreate,
    project: ProjectIdentity = Depends(require_project),
):
    if body.experiment_id and body.experiment_id != experiment_id:
        raise HTTPException(422, "Body experiment_id does not match the URL")
    try:
        return live_state.register_event(
            experiment_id,
            project.id,
            body.model_dump(mode="json", exclude={"experiment_id"}),
        )
    except StateError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@router.get("/{experiment_id}/events")
def list_collapse_events(
    experiment_id: str,
    project: ProjectIdentity = Depends(require_project),
):
    try:
        return {
            "experiment_id": experiment_id,
            "events": live_state.list_events(experiment_id, project.id),
        }
    except StateError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@router.get("/{experiment_id}/events/{event_id}")
def get_collapse_event(
    experiment_id: str,
    event_id: str,
    project: ProjectIdentity = Depends(require_project),
):
    try:
        return live_state.get_event(experiment_id, project.id, event_id)
    except StateError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
