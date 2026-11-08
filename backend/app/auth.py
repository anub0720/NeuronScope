"""Project API-key and dashboard-session authentication."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import Cookie, Depends, Header, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import Project, ProjectAPIKey, User, UserSession

SESSION_COOKIE = "ns_session"
bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class ProjectIdentity:
    id: str
    name: str


@dataclass(frozen=True)
class UserIdentity:
    id: str
    email: str


@dataclass(frozen=True)
class SessionIdentity:
    user: UserIdentity
    session_id: str
    csrf_token_hash: str
    csrf_token: str


def create_api_key() -> str:
    return "ns_live_" + secrets.token_urlsafe(32)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def create_session(db: Session, user: User) -> tuple[str, str, UserSession]:
    raw_session = secrets.token_urlsafe(32)
    csrf_token = _csrf_token(raw_session)
    session = UserSession(
        user_id=user.id,
        token_hash=hash_api_key(raw_session),
        csrf_token_hash=hash_api_key(csrf_token),
        expires_at=datetime.now(timezone.utc)
        + timedelta(seconds=settings.SESSION_TTL_SECONDS),
    )
    db.add(session)
    db.flush()
    return raw_session, csrf_token, session


def _csrf_token(raw_session: str) -> str:
    digest = hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        b"neuronscope-csrf:" + raw_session.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def set_session_cookie(response: Response, raw_session: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        raw_session,
        max_age=settings.SESSION_TTL_SECONDS,
        httponly=True,
        secure=settings.SESSION_COOKIE_SECURE,
        samesite=settings.SESSION_COOKIE_SAMESITE,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        httponly=True,
        secure=settings.SESSION_COOKIE_SECURE,
        samesite=settings.SESSION_COOKIE_SAMESITE,
        path="/",
    )


def _session_identity(raw_session: str | None) -> SessionIdentity | None:
    if not raw_session:
        return None
    db = SessionLocal()
    try:
        session = (
            db.query(UserSession)
            .filter_by(token_hash=hash_api_key(raw_session), revoked_at=None)
            .first()
        )
        now = datetime.now(timezone.utc)
        if session is None:
            return None
        expiry = session.expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry <= now:
            return None
        user = db.query(User).filter_by(id=session.user_id).first()
        if user is None or (
            settings.REQUIRE_EMAIL_VERIFICATION and not user.email_verified
        ):
            return None
        csrf_token = _csrf_token(raw_session)
        if not hmac.compare_digest(
            hash_api_key(csrf_token), session.csrf_token_hash
        ):
            return None
        return SessionIdentity(
            user=UserIdentity(user.id, user.email),
            session_id=session.id,
            csrf_token_hash=session.csrf_token_hash,
            csrf_token=csrf_token,
        )
    finally:
        db.close()


def current_session(
    session_cookie: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> SessionIdentity | None:
    return _session_identity(session_cookie)


def session_identity_from_cookie(session_cookie: str | None) -> SessionIdentity | None:
    return _session_identity(session_cookie)


def owned_project(project_id: str, user_id: str) -> ProjectIdentity | None:
    db = SessionLocal()
    try:
        project = db.query(Project).filter_by(
            id=project_id, owner_user_id=user_id, deleted_at=None
        ).first()
        return ProjectIdentity(project.id, project.name) if project else None
    finally:
        db.close()


def require_user(
    request: Request,
    session: SessionIdentity | None = Depends(current_session),
    csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> UserIdentity:
    if session is None:
        raise HTTPException(status_code=401, detail="Dashboard login required")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin not in settings.DASHBOARD_ORIGINS:
            raise HTTPException(status_code=403, detail="Untrusted dashboard origin")
        if not csrf_token or not hmac.compare_digest(
            hash_api_key(csrf_token), session.csrf_token_hash
        ):
            raise HTTPException(status_code=403, detail="Invalid CSRF token")
    return session.user


def _project_from_key(key: str) -> ProjectIdentity | None:
    token_hash = hash_api_key(key)
    db = SessionLocal()
    try:
        project = db.query(Project).filter_by(
            token_hash=token_hash, deleted_at=None
        ).first()
        if project is None:
            api_key = (
                db.query(ProjectAPIKey)
                .filter_by(token_hash=token_hash, revoked_at=None)
                .first()
            )
            project = (
                db.query(Project).filter_by(
                    id=api_key.project_id, deleted_at=None
                ).first()
                if api_key
                else None
            )
        return ProjectIdentity(project.id, project.name) if project else None
    finally:
        db.close()


def project_for_api_key(key: str) -> ProjectIdentity | None:
    return _project_from_key(key)


def require_project(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    session: SessionIdentity | None = Depends(current_session),
    project_id: str | None = Header(default=None, alias="X-Project-ID"),
    csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> ProjectIdentity:
    if credentials is not None:
        project = _project_from_key(credentials.credentials)
        if project is None:
            raise HTTPException(status_code=401, detail="Invalid API key")
        if project_id and project.id != project_id:
            raise HTTPException(
                status_code=403,
                detail="API key does not belong to the requested project",
            )
        return project
    if session is None or not project_id:
        raise HTTPException(status_code=401, detail="Project API key or dashboard login required")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin not in settings.DASHBOARD_ORIGINS:
            raise HTTPException(status_code=403, detail="Untrusted dashboard origin")
        if not csrf_token or not hmac.compare_digest(
            hash_api_key(csrf_token), session.csrf_token_hash
        ):
            raise HTTPException(status_code=403, detail="Invalid CSRF token")

    db = SessionLocal()
    try:
        project = db.query(Project).filter_by(
            id=project_id,
            owner_user_id=session.user.id,
            deleted_at=None,
        ).first()
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        return ProjectIdentity(project.id, project.name)
    finally:
        db.close()
