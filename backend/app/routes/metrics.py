"""Live metric ingestion and snapshots."""

from fastapi import APIRouter, Depends, HTTPException

from app.auth import ProjectIdentity, require_project
from app.schemas import EpochChunkCreate, EpochCompleteCreate, EpochCreate
from app.state import StateError, live_state

router = APIRouter(prefix="/api/v1/experiments", tags=["Live Metrics"])


@router.post("/{experiment_id}/epochs")
def ingest_epoch(
    experiment_id: str,
    body: EpochCreate,
    project: ProjectIdentity = Depends(require_project),
):
    if body.experiment_id and body.experiment_id != experiment_id:
        raise HTTPException(422, "Body experiment_id does not match the URL")
    try:
        return live_state.ingest_epoch(
            experiment_id,
            project.id,
            body.model_dump(mode="json", exclude={"experiment_id"}),
        )
    except StateError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@router.post("/{experiment_id}/epochs/{epoch}/chunks")
def stage_epoch_chunk(
    experiment_id: str,
    epoch: int,
    body: EpochChunkCreate,
    project: ProjectIdentity = Depends(require_project),
):
    if body.experiment_id and body.experiment_id != experiment_id:
        raise HTTPException(422, "Body experiment_id does not match the URL")
    try:
        return live_state.stage_epoch_chunk(
            experiment_id,
            project.id,
            epoch,
            body.chunk_index,
            body.chunk_count,
            {
                neuron_id: metrics.model_dump(mode="json")
                for neuron_id, metrics in body.neurons.items()
            },
        )
    except StateError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@router.post("/{experiment_id}/epochs/{epoch}/complete")
def complete_staged_epoch(
    experiment_id: str,
    epoch: int,
    body: EpochCompleteCreate,
    project: ProjectIdentity = Depends(require_project),
):
    if body.experiment_id and body.experiment_id != experiment_id:
        raise HTTPException(422, "Body experiment_id does not match the URL")
    try:
        return live_state.complete_staged_epoch(
            experiment_id, project.id, epoch, body.chunk_count
        )
    except StateError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@router.get("/{experiment_id}/live")
def get_live_snapshot(
    experiment_id: str,
    project: ProjectIdentity = Depends(require_project),
):
    try:
        return live_state.snapshot(experiment_id, project.id)
    except StateError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@router.post("/{experiment_id}/batches", include_in_schema=False)
def reject_persistent_metric_batches(
    experiment_id: str,
    project: ProjectIdentity = Depends(require_project),
):
    del experiment_id, project
    raise HTTPException(
        410,
        "Persistent metric batches are disabled; submit live data to /epochs",
    )


@router.get("/{experiment_id}/metrics", include_in_schema=False)
def legacy_metrics_snapshot(
    experiment_id: str,
    project: ProjectIdentity = Depends(require_project),
):
    try:
        return live_state.snapshot(experiment_id, project.id)
    except StateError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
