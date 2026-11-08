"""Password accounts, browser sessions, and SDK device authorization."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.auth import (
    UserIdentity,
    create_session,
    hash_api_key,
    require_user,
    set_session_cookie,
    clear_session_cookie,
    current_session,
)
from app.config import settings
from app.database import get_db
from app.models import (
    AccountToken,
    DeviceAuthorization,
    Project,
    ProjectAPIKey,
    User,
    UserSession,
)
from app.rate_limit import enforce_rate_limit
from app.schemas import (
    AccountLogin,
    AccountEmailRequest,
    AccountRegister,
    AccountTokenRequest,
    DeviceApprovalRequest,
    DeviceAuthorizationCreate,
    DeviceCodeRequest,
    PasswordResetRequest,
    ProjectClaimRequest,
)

router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _email(value: str) -> str:
    return value.strip().casefold()


def _device_project_key(device_code_hash: str) -> str:
    digest = hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        b"neuronscope-device-project-key:" + device_code_hash.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return "ns_live_" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return "scrypt$16384$8$1$" + salt.hex() + "$" + derived.hex()


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_hex, digest_hex = encoded.split("$")
        if algorithm != "scrypt" or int(n) != 2**14 or int(r) != 8 or int(p) != 1:
            return False
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=2**14,
            r=8,
            p=1,
        )
        return secrets.compare_digest(derived.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def _send_email(recipient: str, subject: str, body: str) -> None:
    if not settings.SMTP_HOST or not settings.SMTP_FROM_EMAIL:
        raise RuntimeError("Email delivery is not configured")
    message = EmailMessage()
    message["From"] = settings.SMTP_FROM_EMAIL
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as client:
        if settings.SMTP_USE_TLS:
            client.starttls()
        if settings.SMTP_USERNAME:
            client.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD or "")
        client.send_message(message)


def _new_account_token(
    db: Session, user: User, purpose: str
) -> tuple[str, AccountToken]:
    raw_token = secrets.token_urlsafe(32)
    token = AccountToken(
        user_id=user.id,
        token_hash=hash_api_key(raw_token),
        purpose=purpose,
        expires_at=_now() + timedelta(seconds=settings.ACCOUNT_TOKEN_TTL_SECONDS),
    )
    db.add(token)
    return raw_token, token


def _require_mail_configuration() -> None:
    if not settings.SMTP_HOST or not settings.SMTP_FROM_EMAIL:
        raise HTTPException(503, "Account email delivery is not configured")


def _session_response(response: Response, db: Session, user: User) -> dict:
    raw_session, csrf_token, _session = create_session(db, user)
    db.commit()
    set_session_cookie(response, raw_session)
    return {"user": {"id": user.id, "email": user.email}, "csrf_token": csrf_token}


@router.post("/register")
def register_account(
    body: AccountRegister,
    request: Request,
    db: Session = Depends(get_db),
):
    enforce_rate_limit(request, "register", 5, 3600, body.email)
    _require_mail_configuration()
    email = _email(body.email)
    if "@" not in email:
        raise HTTPException(422, "Enter a valid email address")
    if db.query(User).filter_by(email=email).first():
        raise HTTPException(409, "An account with this email already exists")

    user = User(email=email, password_hash=_hash_password(body.password))
    db.add(user)
    db.flush()
    raw_token, _token = _new_account_token(db, user, "verify_email")
    db.commit()
    verification_url = (
        f"{settings.DASHBOARD_BASE_URL.rstrip('/')}/?verify_token="
        f"{quote(raw_token)}"
    )
    try:
        _send_email(
            email,
            "Verify your NeuronScope account",
            f"Verify your account using this link (valid for one hour):\n{verification_url}\n",
        )
    except (OSError, smtplib.SMTPException, RuntimeError) as exc:
        raise HTTPException(
            503, "Account created, but verification email could not be sent"
        ) from exc
    return {"status": "verification_sent"}


@router.post("/resend-verification")
def resend_verification(
    body: AccountLogin,
    request: Request,
    db: Session = Depends(get_db),
):
    enforce_rate_limit(request, "resend-verification", 5, 3600, body.email)
    _require_mail_configuration()
    user = db.query(User).filter_by(email=_email(body.email)).first()
    if user is None or user.email_verified or not _verify_password(
        body.password, user.password_hash
    ):
        return {"status": "verification_sent"}
    raw_token, _token = _new_account_token(db, user, "verify_email")
    db.commit()
    verification_url = (
        f"{settings.DASHBOARD_BASE_URL.rstrip('/')}/?verify_token="
        f"{quote(raw_token)}"
    )
    try:
        _send_email(
            user.email,
            "Verify your NeuronScope account",
            f"Verify your account using this link:\n{verification_url}\n",
        )
    except (OSError, smtplib.SMTPException, RuntimeError) as exc:
        raise HTTPException(503, "Verification email could not be sent") from exc
    return {"status": "verification_sent"}


@router.post("/verify-email")
def verify_email(
    body: AccountTokenRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    enforce_rate_limit(request, "verify-email", 20, 900)
    token = (
        db.query(AccountToken)
        .filter_by(token_hash=hash_api_key(body.token), purpose="verify_email")
        .first()
    )
    if (
        token is None
        or token.consumed_at is not None
        or _utc(token.expires_at) <= _now()
    ):
        raise HTTPException(400, "Verification link is invalid or expired")
    user = db.query(User).filter_by(id=token.user_id).first()
    if user is None:
        raise HTTPException(400, "Verification link is invalid or expired")
    user.email_verified = True
    token.consumed_at = _now()
    db.commit()
    return {"status": "verified"}


@router.post("/login")
def login(
    body: AccountLogin,
    response: Response,
    request: Request,
    db: Session = Depends(get_db),
):
    enforce_rate_limit(request, "login", 10, 900, body.email)
    user = db.query(User).filter_by(email=_email(body.email)).first()
    if user is None:
        _verify_password(body.password, _hash_password("dummy-password-value"))
        raise HTTPException(401, "Email or password is incorrect")
    if user.locked_until and _utc(user.locked_until) > _now():
        raise HTTPException(429, "Too many failed sign-in attempts; try again later")
    if not _verify_password(body.password, user.password_hash):
        user.failed_login_attempts += 1
        if user.failed_login_attempts >= 10:
            user.failed_login_attempts = 0
            user.locked_until = _now() + timedelta(minutes=15)
        db.commit()
        raise HTTPException(401, "Email or password is incorrect")
    if not user.email_verified:
        raise HTTPException(403, "Verify your email before signing in")
    user.failed_login_attempts = 0
    user.locked_until = None
    db.flush()
    result = _session_response(response, db, user)
    return result


@router.get("/session")
def get_session(
    session=Depends(current_session),
    db: Session = Depends(get_db),
):
    if session is None:
        raise HTTPException(401, "Dashboard login required")
    user = db.query(User).filter_by(id=session.user.id).first()
    record = db.query(UserSession).filter_by(id=session.session_id).first()
    if user is None or record is None:
        raise HTTPException(401, "Dashboard session expired")
    return {
        "user": {"id": user.id, "email": user.email},
        "csrf_token": session.csrf_token,
    }


@router.post("/logout")
def logout(
    response: Response,
    session=Depends(current_session),
    _user: UserIdentity = Depends(require_user),
    db: Session = Depends(get_db),
):
    if session is not None:
        record = db.query(UserSession).filter_by(id=session.session_id).first()
        if record:
            record.revoked_at = _now()
            db.commit()
    clear_session_cookie(response)
    return {"status": "logged_out"}


@router.post("/password-reset/request")
def request_password_reset(
    body: AccountEmailRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    enforce_rate_limit(request, "password-reset", 5, 3600, body.email)
    _require_mail_configuration()
    email = _email(body.email)
    user = db.query(User).filter_by(email=email).first()
    if user and user.email_verified:
        raw_token, _token = _new_account_token(db, user, "password_reset")
        db.commit()
        reset_url = (
            f"{settings.DASHBOARD_BASE_URL.rstrip('/')}/?reset_token="
            f"{quote(raw_token)}"
        )
        try:
            _send_email(
                email,
                "Reset your NeuronScope password",
                f"Reset your password using this one-time link:\n{reset_url}\n",
            )
        except (OSError, smtplib.SMTPException, RuntimeError) as exc:
            raise HTTPException(503, "Password reset email could not be sent") from exc
    return {"status": "if_account_exists_email_sent"}


@router.post("/password-reset/complete")
def complete_password_reset(
    body: PasswordResetRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    enforce_rate_limit(request, "password-reset-complete", 10, 900)
    token = (
        db.query(AccountToken)
        .filter_by(token_hash=hash_api_key(body.token), purpose="password_reset")
        .first()
    )
    if (
        token is None
        or token.consumed_at is not None
        or _utc(token.expires_at) <= _now()
    ):
        raise HTTPException(400, "Password reset link is invalid or expired")
    user = db.query(User).filter_by(id=token.user_id).first()
    if user is None or not user.email_verified:
        raise HTTPException(400, "Password reset link is invalid or expired")
    user.password_hash = _hash_password(body.new_password)
    token.consumed_at = _now()
    db.query(UserSession).filter_by(user_id=user.id, revoked_at=None).update(
        {"revoked_at": _now()}
    )
    db.commit()
    return {"status": "password_reset"}


@router.post("/device")
def create_device_authorization(
    body: DeviceAuthorizationCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    enforce_rate_limit(request, "device-create", 20, 3600)
    active_count = (
        db.query(DeviceAuthorization)
        .filter(
            DeviceAuthorization.expires_at > _now(),
            DeviceAuthorization.consumed_at.is_(None),
        )
        .count()
    )
    if active_count >= 500:
        raise HTTPException(429, "Device authorization capacity reached")
    device_code = secrets.token_urlsafe(32)
    user_code = "".join(
        secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8)
    )
    record = DeviceAuthorization(
        device_code_hash=hash_api_key(device_code),
        user_code_hash=hash_api_key(user_code),
        requested_project_id=body.project_id,
        project_name=body.project_name.strip(),
        expires_at=_now() + timedelta(seconds=settings.DEVICE_AUTH_TTL_SECONDS),
    )
    db.add(record)
    db.commit()
    return {
        "device_code": device_code,
        "user_code": user_code,
        "verification_uri": f"{settings.DASHBOARD_BASE_URL.rstrip('/')}/?device_code={quote(user_code)}",
        "expires_in": settings.DEVICE_AUTH_TTL_SECONDS,
        "interval": 2,
    }


@router.post("/device/approve")
def approve_device(
    body: DeviceApprovalRequest,
    request: Request,
    user: UserIdentity = Depends(require_user),
    db: Session = Depends(get_db),
):
    enforce_rate_limit(request, "device-approve", 20, 3600, user.id)
    record = (
        db.query(DeviceAuthorization)
        .filter_by(user_code_hash=hash_api_key(body.user_code.strip().upper()))
        .first()
    )
    if (
        record is None
        or record.consumed_at is not None
        or _utc(record.expires_at) <= _now()
    ):
        raise HTTPException(400, "Device code is invalid or expired")
    if record.approved_user_id is not None:
        raise HTTPException(409, "Device authorization was already approved")
    if record.requested_project_id:
        project = (
            db.query(Project)
            .filter_by(
                id=record.requested_project_id,
                owner_user_id=user.id,
                deleted_at=None,
            )
            .first()
        )
        if project is None:
            raise HTTPException(404, "Project not found or not owned by this account")
    record.approved_user_id = user.id
    db.commit()
    return {"status": "approved"}


@router.post("/device/token")
def poll_device_token(
    body: DeviceCodeRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    enforce_rate_limit(request, "device-token", 40, 60, body.device_code)
    record = (
        db.query(DeviceAuthorization)
        .filter_by(device_code_hash=hash_api_key(body.device_code))
        .with_for_update()
        .first()
    )
    if record is None or _utc(record.expires_at) <= _now():
        raise HTTPException(410, "Device authorization expired")
    if record.consumed_at is not None:
        if record.provisioned_project_id is None:
            raise HTTPException(410, "Device authorization was already used")
        project = db.query(Project).filter_by(
            id=record.provisioned_project_id, deleted_at=None
        ).first()
        if project is None:
            raise HTTPException(410, "Provisioned project is no longer available")
        credential = (
            db.query(ProjectAPIKey)
            .filter_by(
                project_id=project.id,
                token_hash=hash_api_key(
                    _device_project_key(record.device_code_hash)
                ),
                revoked_at=None,
            )
            .first()
        )
        if credential is None:
            raise HTTPException(410, "The issued project credential was revoked")
        return {
            "project_id": project.id,
            "project_name": project.name,
            "api_key": _device_project_key(record.device_code_hash),
        }
    if record.approved_user_id is None:
        raise HTTPException(428, "Authorization is pending")
    user = db.query(User).filter_by(
        id=record.approved_user_id, email_verified=True
    ).first()
    if user is None:
        raise HTTPException(403, "Verified account required")

    if record.requested_project_id:
        project = (
            db.query(Project)
            .filter_by(
                id=record.requested_project_id,
                owner_user_id=user.id,
                deleted_at=None,
            )
            .first()
        )
        if project is None:
            raise HTTPException(404, "Project not found or not owned by this account")
    else:
        project = (
            db.query(Project)
            .filter_by(owner_user_id=user.id, deleted_at=None)
            .order_by(Project.created_at.asc())
            .first()
        )
    api_key = _device_project_key(record.device_code_hash)
    if project is None:
        project = Project(
            name=record.project_name,
            description=None,
            token_hash=None,
            owner_user_id=user.id,
        )
        db.add(project)
        db.flush()
    db.add(
        ProjectAPIKey(
            project_id=project.id,
            name="SDK authorization",
            token_hash=hash_api_key(api_key),
        )
    )
    record.provisioned_project_id = project.id
    record.consumed_at = _now()
    db.commit()
    return {
        "project_id": project.id,
        "project_name": project.name,
        "api_key": api_key,
    }


@router.post("/projects/claim")
def claim_legacy_project(
    body: ProjectClaimRequest,
    user: UserIdentity = Depends(require_user),
    db: Session = Depends(get_db),
):
    key_hash = hash_api_key(body.api_key)
    project = (
        db.query(Project)
        .filter_by(token_hash=key_hash, deleted_at=None)
        .first()
    )
    if project is None:
        key_record = db.query(ProjectAPIKey).filter_by(
            token_hash=key_hash, revoked_at=None
        ).first()
        project = (
            db.query(Project)
            .filter_by(id=key_record.project_id, deleted_at=None)
            .first()
            if key_record
            else None
        )
    if project is None:
        raise HTTPException(401, "Invalid project API key")
    if project.owner_user_id not in {None, user.id}:
        raise HTTPException(409, "Project is already owned by another account")
    project.owner_user_id = user.id
    db.commit()
    return {"project_id": project.id, "name": project.name, "status": "claimed"}
