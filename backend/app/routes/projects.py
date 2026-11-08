from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.auth import (
    UserIdentity,
    create_api_key,
    hash_api_key,
    require_user,
)
from app.database import get_db
from app.models import Experiment, Project, ProjectAPIKey
from app.rate_limit import enforce_rate_limit
from app.schemas import ProjectCreate, ProjectCredentialCreate, ProjectUpdate
from app.state import StateError, live_state

router = APIRouter(prefix="/api/v1/projects", tags=["Projects"])


def _owned_project(db: Session, project_id: str, user_id: str) -> Project:
    project = (
        db.query(Project)
        .filter_by(id=project_id, owner_user_id=user_id, deleted_at=None)
        .first()
    )
    if project is None:
        raise HTTPException(404, "Project not found")
    return project


def _credential_payload(credential: ProjectAPIKey) -> dict:
    return {
        "credential_id": credential.id,
        "name": credential.name,
        "created_at": credential.created_at.isoformat(),
        "revoked_at": (
            credential.revoked_at.isoformat() if credential.revoked_at else None
        ),
        "is_legacy": False,
    }


@router.get("")
def list_projects(
    user: UserIdentity = Depends(require_user),
    db: Session = Depends(get_db),
):
    projects = (
        db.query(Project)
        .filter_by(owner_user_id=user.id, deleted_at=None)
        .order_by(Project.created_at.desc())
        .all()
    )
    return [
        {
            "project_id": project.id,
            "name": project.name,
            "description": project.description,
            "created_at": project.created_at.isoformat(),
        }
        for project in projects
    ]


@router.post("")
def create_project(
    body: ProjectCreate,
    request: Request,
    user: UserIdentity = Depends(require_user),
    db: Session = Depends(get_db),
):
    enforce_rate_limit(request, "project-create", 20, 3600, user.id)
    project = Project(
        name=body.name,
        description=body.description,
        token_hash=None,
        owner_user_id=user.id,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return {
        "project_id": project.id,
        "name": project.name,
        "description": project.description,
        "created_at": project.created_at.isoformat(),
    }


@router.patch("/{project_id}")
def rename_project(
    project_id: str,
    body: ProjectUpdate,
    user: UserIdentity = Depends(require_user),
    db: Session = Depends(get_db),
):
    project = _owned_project(db, project_id, user.id)
    project.name = body.name
    db.commit()
    db.refresh(project)
    return {
        "project_id": project.id,
        "name": project.name,
        "description": project.description,
        "created_at": project.created_at.isoformat(),
    }


@router.delete("/{project_id}")
def archive_project(
    project_id: str,
    user: UserIdentity = Depends(require_user),
    db: Session = Depends(get_db),
):
    project = _owned_project(db, project_id, user.id)
    now = datetime.now(timezone.utc)
    project.deleted_at = now
    project.token_hash = None
    db.query(ProjectAPIKey).filter_by(
        project_id=project.id, revoked_at=None
    ).update({"revoked_at": now}, synchronize_session=False)
    experiments = db.query(Experiment).filter_by(project_id=project.id).all()
    experiment_ids = [experiment.id for experiment in experiments]
    db.commit()
    for experiment_id in experiment_ids:
        try:
            live_state.delete_experiment(experiment_id, project.id)
        except StateError as exc:
            if exc.status_code != 404:
                raise HTTPException(exc.status_code, exc.detail) from exc
    return {"status": "archived", "project_id": project.id}


@router.get("/{project_id}/credentials")
def list_project_credentials(
    project_id: str,
    user: UserIdentity = Depends(require_user),
    db: Session = Depends(get_db),
):
    project = _owned_project(db, project_id, user.id)
    credentials = (
        db.query(ProjectAPIKey)
        .filter_by(project_id=project.id)
        .order_by(ProjectAPIKey.created_at.desc())
        .all()
    )
    result = [_credential_payload(credential) for credential in credentials]
    if project.token_hash is not None:
        result.append(
            {
                "credential_id": "legacy",
                "name": "Legacy project key",
                "created_at": project.created_at.isoformat(),
                "revoked_at": None,
                "is_legacy": True,
            }
        )
    return result


@router.post("/{project_id}/credentials")
def create_project_credential(
    project_id: str,
    body: ProjectCredentialCreate,
    request: Request,
    user: UserIdentity = Depends(require_user),
    db: Session = Depends(get_db),
):
    enforce_rate_limit(request, "credential-issue", 20, 3600, user.id)
    project = _owned_project(db, project_id, user.id)
    api_key = create_api_key()
    credential = ProjectAPIKey(
        project_id=project.id,
        name=body.name,
        token_hash=hash_api_key(api_key),
    )
    db.add(credential)
    db.commit()
    db.refresh(credential)
    return {
        **_credential_payload(credential),
        "project_id": project.id,
        "api_key": api_key,
    }


@router.post("/{project_id}/credentials/{credential_id}/rotate")
def rotate_project_credential(
    project_id: str,
    credential_id: str,
    body: ProjectCredentialCreate,
    request: Request,
    user: UserIdentity = Depends(require_user),
    db: Session = Depends(get_db),
):
    enforce_rate_limit(request, "credential-issue", 20, 3600, user.id)
    project = _owned_project(db, project_id, user.id)
    now = datetime.now(timezone.utc)
    if credential_id == "legacy":
        if project.token_hash is None:
            raise HTTPException(404, "Legacy project credential not found")
        project.token_hash = None
    else:
        credential = (
            db.query(ProjectAPIKey)
            .filter_by(id=credential_id, project_id=project.id)
            .first()
        )
        if credential is None:
            raise HTTPException(404, "Project credential not found")
        if credential.revoked_at is not None:
            raise HTTPException(409, "A revoked credential cannot be rotated")
        credential.revoked_at = now

    api_key = create_api_key()
    replacement = ProjectAPIKey(
        project_id=project.id,
        name=body.name,
        token_hash=hash_api_key(api_key),
    )
    db.add(replacement)
    db.commit()
    db.refresh(replacement)
    return {
        **_credential_payload(replacement),
        "project_id": project.id,
        "api_key": api_key,
    }


@router.delete("/{project_id}/credentials/{credential_id}")
def revoke_project_credential(
    project_id: str,
    credential_id: str,
    user: UserIdentity = Depends(require_user),
    db: Session = Depends(get_db),
):
    project = _owned_project(db, project_id, user.id)
    if credential_id == "legacy":
        if project.token_hash is None:
            raise HTTPException(404, "Legacy project credential not found")
        project.token_hash = None
    else:
        credential = (
            db.query(ProjectAPIKey)
            .filter_by(id=credential_id, project_id=project.id)
            .first()
        )
        if credential is None:
            raise HTTPException(404, "Project credential not found")
        if credential.revoked_at is not None:
            raise HTTPException(409, "Project credential is already revoked")
        credential.revoked_at = datetime.now(timezone.utc)
    db.commit()
    return {"status": "revoked", "credential_id": credential_id}
