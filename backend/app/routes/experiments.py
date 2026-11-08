
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import (
    Architecture,
    DatasetClass,
    Experiment,
    Layer,
    Neuron,
)
from app.schemas import ExperimentCreate, ArchitectureCreate
from app.auth import ProjectIdentity, require_project
from app.state import StateError, live_state

router = APIRouter(prefix="/api/v1/experiments", tags=["Experiments"])


@router.post("")
def create_experiment(
    body: ExperimentCreate,
    project: ProjectIdentity = Depends(require_project),
    db: Session = Depends(get_db),
):
    if body.experiment_id:
        try:
            active = live_state.get(body.experiment_id, project.id)
            return {
                "experiment_id": active.experiment_id,
                "name": active.name,
                "status": "finished" if active.finished else "running",
                "duplicate": True,
            }
        except StateError:
            pass

        existing = db.query(Experiment).filter_by(id=body.experiment_id).first()
        if existing is not None:
            if existing.project_id != project.id:
                raise HTTPException(404, "Experiment not found")
            if existing.status == "finished":
                raise HTTPException(409, "Finished experiments cannot be resumed")
            try:
                live_state.create_experiment(
                    experiment_id=existing.id,
                    project_id=project.id,
                    name=existing.name,
                    framework=existing.framework,
                    model_name=existing.model_name,
                )
            except StateError as exc:
                raise HTTPException(exc.status_code, exc.detail) from exc
            return {
                "experiment_id": existing.id,
                "name": existing.name,
                "status": existing.status,
                "resumed": True,
            }

    experiment = Experiment(
        id=body.experiment_id,
        project_id=project.id,
        name=body.name,
        framework=body.framework,
        model_name=body.model_name,
        seed=body.seed,
        config=body.config,
    )

    db.add(experiment)
    db.flush()
    try:
        live_state.create_experiment(
            experiment_id=experiment.id,
            project_id=project.id,
            name=experiment.name,
            framework=experiment.framework,
            model_name=experiment.model_name,
        )
    except StateError as exc:
        db.rollback()
        raise HTTPException(exc.status_code, exc.detail) from exc
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        live_state.discard_uncommitted_experiment(experiment.id)
        raise

    return {
        "experiment_id": experiment.id,
        "name": experiment.name,
        "status": experiment.status,
    }


@router.post("/{experiment_id}/architecture")
def register_architecture(
    experiment_id: str,
    body: ArchitectureCreate,
    project: ProjectIdentity = Depends(require_project),
    db: Session = Depends(get_db),
):
    experiment = db.query(Experiment).filter_by(
        id=experiment_id,
        project_id=project.id,
    ).first()
    if not experiment:
        raise HTTPException(404, "Experiment not found")

    if body.experiment_id and body.experiment_id != experiment_id:
        raise HTTPException(422, "Body experiment_id does not match the URL")
    architecture_data = body.model_dump(
        mode="json", exclude={"experiment_id"}
    )
    try:
        state = live_state.get(experiment_id, project.id)
        if state.finished:
            raise StateError(409, "Finished experiments cannot be modified")
        if state.architecture is not None:
            duplicate = state.architecture == architecture_data
            if not duplicate:
                raise StateError(409, "Experiment architecture is already registered")
            return {
                "status": "registered",
                "duplicate": True,
                "layer_count": len(body.layers),
            }
        neuron_total = sum(layer.neuron_count for layer in body.layers)
        if neuron_total > settings.MAX_NEURONS_PER_EXPERIMENT:
            raise StateError(413, "Architecture exceeds the neuron limit")
    except StateError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc

    manifest = {
        "framework": body.framework,
        "model_name": body.model_name,
        "classes": body.classes,
    }
    existing_architecture = db.query(Architecture).filter_by(
        experiment_id=experiment.id,
        version=body.version,
    ).first()
    if existing_architecture:
        persisted_layers = (
            db.query(Layer)
            .filter_by(architecture_id=existing_architecture.id)
            .order_by(Layer.position)
            .all()
        )
        persisted_architecture_matches = len(persisted_layers) == len(body.layers)
        if persisted_architecture_matches:
            for persisted_layer, requested_layer in zip(
                persisted_layers, body.layers
            ):
                persisted_neurons = (
                    db.query(Neuron)
                    .filter_by(layer_id=persisted_layer.id)
                    .order_by(Neuron.unit_index)
                    .all()
                )
                requested_ids = requested_layer.neuron_ids or [
                    f"{requested_layer.layer_id}:{index}"
                    for index in range(requested_layer.neuron_count)
                ]
                if (
                    persisted_layer.layer_key != requested_layer.layer_id
                    or persisted_layer.module_type != requested_layer.type
                    or persisted_layer.unit_count != requested_layer.neuron_count
                    or [neuron.stable_key for neuron in persisted_neurons]
                    != requested_ids
                ):
                    persisted_architecture_matches = False
                    break
        if (
            existing_architecture.model_signature != body.model_name
            or existing_architecture.manifest != manifest
            or not persisted_architecture_matches
        ):
            raise HTTPException(
                409,
                "Architecture version already exists with different metadata",
            )
        try:
            live_state.register_architecture(
                experiment_id, project.id, architecture_data
            )
        except StateError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc
        return {
            "status": "registered",
            "duplicate": True,
            "architecture_id": existing_architecture.id,
            "layer_count": len(body.layers),
        }

    architecture = Architecture(
        experiment_id=experiment.id,
        version=body.version,
        model_signature=body.model_name,
        manifest=manifest,
    )
    db.add(architecture)
    db.flush()
    for position, layer_data in enumerate(body.layers):
        layer = Layer(
            architecture_id=architecture.id,
            layer_key=layer_data.layer_id,
            display_name=layer_data.layer_id,
            module_type=layer_data.type,
            unit_type="neuron",
            unit_count=layer_data.neuron_count,
            position=position,
            metadata_json={"neuron_ids": layer_data.neuron_ids},
        )
        db.add(layer)
        db.flush()
        db.add_all(
            [
                Neuron(
                    layer_id=layer.id,
                    unit_index=index,
                    stable_key=(
                        layer_data.neuron_ids[index]
                        if layer_data.neuron_ids is not None
                        else f"{layer_data.layer_id}:{index}"
                    ),
                    unit_type="neuron",
                )
                for index in range(layer_data.neuron_count)
            ]
        )
    db.add_all(
        [
            DatasetClass(
                experiment_id=experiment.id,
                class_key=index,
                display_name=label,
            )
            for index, label in body.classes.items()
        ]
    )
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise

    try:
        duplicate = live_state.register_architecture(
            experiment_id, project.id, architecture_data
        )
    except StateError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc

    return {
        "status": "registered",
        "duplicate": duplicate,
        "architecture_id": architecture.id,
        "layer_count": len(body.layers),
    }


@router.get("")
def list_experiments(
    project: ProjectIdentity = Depends(require_project),
    db: Session = Depends(get_db),
):
    experiments = db.query(Experiment).filter_by(
        project_id=project.id
    ).order_by(Experiment.created_at.desc()).all()
    active_summaries = live_state.summaries(project.id)

    return [
        {
            "experiment_id": e.id,
            "name": e.name,
            "status": active_summaries.get(e.id, {}).get("status", e.status),
            "latest_epoch": active_summaries.get(e.id, {}).get(
                "current_epoch", e.latest_epoch
            ),
            "framework": e.framework,
        }
        for e in experiments
    ]


@router.get("/{experiment_id}")
def get_experiment(
    experiment_id: str,
    project: ProjectIdentity = Depends(require_project),
    db: Session = Depends(get_db),
):
    experiment = db.query(Experiment).filter_by(
        id=experiment_id, project_id=project.id
    ).first()
    if experiment is None:
        raise HTTPException(404, "Experiment not found")
    try:
        snapshot = live_state.snapshot(experiment_id, project.id)
    except StateError:
        snapshot = None
    return {
        "experiment_id": experiment.id,
        "project_id": experiment.project_id,
        "name": experiment.name,
        "framework": experiment.framework,
        "model_name": experiment.model_name,
        "status": snapshot["status"] if snapshot else experiment.status,
        "current_epoch": (
            snapshot["current_epoch"] if snapshot else experiment.latest_epoch
        ),
        "architecture_registered": bool(
            snapshot and snapshot["architecture"] is not None
        ),
        "last_activity": snapshot["last_activity"] if snapshot else None,
    }


@router.post("/{experiment_id}/finish")
def finish_experiment(
    experiment_id: str,
    project: ProjectIdentity = Depends(require_project),
    db: Session = Depends(get_db),
):
    experiment = db.query(Experiment).filter_by(
        id=experiment_id, project_id=project.id
    ).first()
    if experiment is None:
        raise HTTPException(404, "Experiment not found")
    try:
        result = live_state.finish_experiment(experiment_id, project.id)
    except StateError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    experiment.status = "finished"
    experiment.completed_at = datetime.now(timezone.utc)
    db.commit()
    return result


@router.delete("/{experiment_id}/live")
def clear_live_metrics(
    experiment_id: str,
    project: ProjectIdentity = Depends(require_project),
):
    try:
        return live_state.clear_live(experiment_id, project.id)
    except StateError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@router.delete("/{experiment_id}/events")
def clear_collapse_events(
    experiment_id: str,
    project: ProjectIdentity = Depends(require_project),
):
    try:
        return live_state.clear_events(experiment_id, project.id)
    except StateError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@router.delete("/{experiment_id}")
def delete_experiment_live_state(
    experiment_id: str,
    project: ProjectIdentity = Depends(require_project),
    db: Session = Depends(get_db),
):
    experiment = db.query(Experiment).filter_by(
        id=experiment_id, project_id=project.id
    ).first()
    if experiment is None:
        raise HTTPException(404, "Experiment not found")
    try:
        return live_state.delete_experiment(experiment_id, project.id)
    except StateError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
