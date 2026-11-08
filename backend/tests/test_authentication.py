import re
import unittest
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from fastapi.testclient import TestClient
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.websockets import WebSocketDisconnect
from pydantic import ValidationError

from app import auth
from app.config import Settings, settings
from app.database import Base, get_db
from app.main import app
from app.models import (
    AccountToken,
    Experiment,
    Project,
    ProjectAPIKey,
    User,
    UserSession,
)
from app.routes import authentication
from app.rate_limit import reset_rate_limits
from app.state import live_state


class AuthenticationIntegrationTests(unittest.TestCase):
    def test_verification_configuration_defaults_secure_and_requires_domain_allowlist(
        self,
    ):
        config = {
            "_env_file": None,
            "DATABASE_URL": "sqlite:///test.db",
            "SECRET_KEY": "test-secret-" * 5,
        }
        self.assertTrue(Settings(**config).REQUIRE_EMAIL_VERIFICATION)
        with self.assertRaises(ValidationError):
            Settings(**config, REQUIRE_EMAIL_VERIFICATION=False)
        self.assertEqual(Settings(**config).SESSION_COOKIE_SAMESITE, "lax")
        with self.assertRaises(ValidationError):
            Settings(
                **config,
                SESSION_COOKIE_SECURE=False,
                SESSION_COOKIE_SAMESITE="none",
            )

        private_team = Settings(
            **config,
            REQUIRE_EMAIL_VERIFICATION=False,
            ALLOWED_REGISTRATION_EMAIL_DOMAINS=["Team.Example"],
        )
        self.assertEqual(
            private_team.ALLOWED_REGISTRATION_EMAIL_DOMAINS,
            ["team.example"],
        )

    def setUp(self):
        reset_rate_limits()
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(
            autocommit=False, autoflush=False, bind=self.engine
        )
        self.original_session_local = auth.SessionLocal
        auth.SessionLocal = self.sessions
        self.original_settings = {
            "SMTP_HOST": settings.SMTP_HOST,
            "SMTP_FROM_EMAIL": settings.SMTP_FROM_EMAIL,
            "DASHBOARD_BASE_URL": settings.DASHBOARD_BASE_URL,
            "DASHBOARD_ORIGINS": settings.DASHBOARD_ORIGINS,
            "SESSION_COOKIE_SECURE": settings.SESSION_COOKIE_SECURE,
            "SESSION_COOKIE_SAMESITE": settings.SESSION_COOKIE_SAMESITE,
            "SESSION_TTL_SECONDS": settings.SESSION_TTL_SECONDS,
            "REQUIRE_EMAIL_VERIFICATION": settings.REQUIRE_EMAIL_VERIFICATION,
            "ALLOWED_REGISTRATION_EMAIL_DOMAINS": (
                settings.ALLOWED_REGISTRATION_EMAIL_DOMAINS
            ),
        }
        settings.REQUIRE_EMAIL_VERIFICATION = True
        settings.ALLOWED_REGISTRATION_EMAIL_DOMAINS = []
        settings.SMTP_HOST = "smtp.test"
        settings.SMTP_FROM_EMAIL = "noreply@example.test"
        settings.DASHBOARD_BASE_URL = "http://testserver"
        settings.DASHBOARD_ORIGINS = ["http://testserver"]
        settings.SESSION_COOKIE_SECURE = False
        settings.SESSION_COOKIE_SAMESITE = "lax"
        self.sent_emails = []
        self.original_sender = authentication._send_email
        authentication._send_email = (
            lambda recipient, subject, body: self.sent_emails.append(
                (recipient, subject, body)
            )
        )

        def override_db():
            db = self.sessions()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_db
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.clear()
        authentication._send_email = self.original_sender
        auth.SessionLocal = self.original_session_local
        for name, value in self.original_settings.items():
            setattr(settings, name, value)
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _register_and_verify(self, email="owner@example.test"):
        response = self.client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "a secure password 123"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        token = re.search(r"verify_token=([^\s]+)", self.sent_emails[-1][2])
        self.assertIsNotNone(token)
        verify = self.client.post(
            "/api/v1/auth/verify-email",
            json={"token": token.group(1)},
        )
        self.assertEqual(verify.status_code, 200, verify.text)
        return email

    def _login(self, email="owner@example.test"):
        response = self.client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "a secure password 123"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["csrf_token"]

    def test_account_verification_cookie_session_and_device_project_reuse(self):
        email = self._register_and_verify()
        csrf = self._login(email)
        session_cookie = self.client.cookies.get("ns_session")
        self.assertTrue(session_cookie)
        self.assertTrue(self.client.cookies.get("ns_session"))
        me = self.client.get("/api/v1/auth/session")
        self.assertEqual(me.status_code, 200)
        csrf = me.json()["csrf_token"]

        first_device = self.client.post(
            "/api/v1/auth/device",
            json={"project_name": "SDK-owned project"},
        ).json()
        pending = self.client.post(
            "/api/v1/auth/device/token",
            json={"device_code": first_device["device_code"]},
        )
        self.assertEqual(pending.status_code, 428)
        approved = self.client.post(
            "/api/v1/auth/device/approve",
            json={"user_code": first_device["user_code"]},
            headers={"Origin": "http://testserver", "X-CSRF-Token": csrf},
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        provisioned = self.client.post(
            "/api/v1/auth/device/token",
            json={"device_code": first_device["device_code"]},
        )
        self.assertEqual(provisioned.status_code, 200, provisioned.text)
        credentials = provisioned.json()
        self.assertTrue(credentials["api_key"].startswith("ns_live_"))
        self.assertIsNotNone(auth.project_for_api_key(credentials["api_key"]))
        retried = self.client.post(
            "/api/v1/auth/device/token",
            json={"device_code": first_device["device_code"]},
        )
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertEqual(retried.json(), credentials)

        projects = self.client.get("/api/v1/projects")
        self.assertEqual([item["project_id"] for item in projects.json()], [credentials["project_id"]])
        project = self.client.get("/api/v1/experiments", headers={
            "X-Project-ID": credentials["project_id"]
        })
        self.assertEqual(project.status_code, 200, project.text)
        self.assertEqual(project.json(), [])

        second_device = self.client.post(
            "/api/v1/auth/device",
            json={"project_name": "Should reuse"},
        ).json()
        self.client.post(
            "/api/v1/auth/device/approve",
            json={"user_code": second_device["user_code"]},
            headers={"Origin": "http://testserver", "X-CSRF-Token": csrf},
        )
        second_credentials = self.client.post(
            "/api/v1/auth/device/token",
            json={"device_code": second_device["device_code"]},
        ).json()
        self.assertEqual(second_credentials["project_id"], credentials["project_id"])
        self.assertNotEqual(second_credentials["api_key"], credentials["api_key"])
        self.assertIsNotNone(auth.project_for_api_key(credentials["api_key"]))
        self.assertIsNotNone(auth.project_for_api_key(second_credentials["api_key"]))

    def test_cross_site_session_cookie_survives_reload_and_expires_with_server_session(self):
        email = self._register_and_verify()
        settings.SESSION_COOKIE_SECURE = True
        settings.SESSION_COOKIE_SAMESITE = "none"
        settings.SESSION_TTL_SECONDS = 1800

        with TestClient(app, base_url="https://testserver") as client:
            response = client.post(
                "/api/v1/auth/login",
                json={"email": email, "password": "a secure password 123"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("ns_session=", response.headers["set-cookie"])

            cookie = SimpleCookie()
            cookie.load(response.headers["set-cookie"])
            session_cookie = cookie["ns_session"]
            self.assertEqual(session_cookie["path"], "/")
            self.assertEqual(session_cookie["domain"], "")
            self.assertEqual(session_cookie["max-age"], "1800")
            self.assertEqual(session_cookie["samesite"], "none")
            self.assertTrue(session_cookie["secure"])
            self.assertTrue(session_cookie["httponly"])

            db = self.sessions()
            try:
                session = db.query(UserSession).one()
                expiry = session.expires_at
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                remaining = (expiry - datetime.now(timezone.utc)).total_seconds()
                self.assertGreater(remaining, 1790)
                self.assertLessEqual(remaining, 1800)
            finally:
                db.close()

            self.assertEqual(client.get("/api/v1/auth/session").status_code, 200)
            self.assertEqual(client.get("/api/v1/projects").status_code, 200)

            reloaded_client = TestClient(app, base_url="https://testserver")
            reloaded_client.cookies.update(client.cookies)
            self.assertEqual(
                reloaded_client.get("/api/v1/auth/session").status_code, 200
            )
            self.assertEqual(
                reloaded_client.get("/api/v1/projects").status_code, 200
            )

            db = self.sessions()
            try:
                session = db.query(UserSession).one()
                session.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                db.commit()
            finally:
                db.close()
            self.assertEqual(
                reloaded_client.get("/api/v1/auth/session").status_code, 401
            )
            reloaded_client.close()

    def test_cors_preflight_allows_configured_origin_with_credentials(self):
        cors = next(
            middleware
            for middleware in app.user_middleware
            if middleware.cls is CORSMiddleware
        )
        origin = cors.kwargs["allow_origins"][0]
        response = self.client.options(
            "/api/v1/auth/login",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["access-control-allow-origin"], origin)
        self.assertEqual(response.headers["access-control-allow-credentials"], "true")

    def test_disabled_verification_supports_team_signup_login_resend_reset_and_device_flow(self):
        settings.REQUIRE_EMAIL_VERIFICATION = False
        settings.ALLOWED_REGISTRATION_EMAIL_DOMAINS = ["example.test"]
        settings.SMTP_HOST = None
        settings.SMTP_FROM_EMAIL = None

        registered = self.client.post(
            "/api/v1/auth/register",
            json={
                "email": "new-member@example.test",
                "password": "a secure password 123",
            },
        )
        self.assertEqual(registered.status_code, 200, registered.text)
        self.assertEqual(registered.json(), {"status": "registered"})
        self.assertEqual(self.sent_emails, [])

        with self.sessions() as db:
            new_user = db.query(User).filter_by(
                email="new-member@example.test"
            ).one()
            self.assertFalse(new_user.email_verified)
            self.assertEqual(
                db.query(AccountToken).filter_by(
                    user_id=new_user.id, purpose="verify_email"
                ).count(),
                0,
            )

        login = self.client.post(
            "/api/v1/auth/login",
            json={
                "email": "new-member@example.test",
                "password": "a secure password 123",
            },
        )
        self.assertEqual(login.status_code, 200, login.text)
        self.assertEqual(
            self.client.get("/api/v1/auth/session").status_code,
            200,
        )

        resend = self.client.post(
            "/api/v1/auth/resend-verification",
            json={
                "email": "new-member@example.test",
                "password": "a secure password 123",
            },
        )
        self.assertEqual(
            resend.json(),
            {"status": "verification_not_required"},
        )
        self.assertEqual(self.sent_emails, [])

        device = self.client.post(
            "/api/v1/auth/device",
            json={"project_name": "Unverified team member"},
        ).json()
        csrf = login.json()["csrf_token"]
        approved = self.client.post(
            "/api/v1/auth/device/approve",
            json={"user_code": device["user_code"]},
            headers={"Origin": "http://testserver", "X-CSRF-Token": csrf},
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        token = self.client.post(
            "/api/v1/auth/device/token",
            json={"device_code": device["device_code"]},
        )
        self.assertEqual(token.status_code, 200, token.text)
        self.assertTrue(token.json()["api_key"].startswith("ns_live_"))

        settings.SMTP_HOST = "smtp.test"
        settings.SMTP_FROM_EMAIL = "noreply@example.test"
        reset_requested = self.client.post(
            "/api/v1/auth/password-reset/request",
            json={"email": "new-member@example.test"},
        )
        self.assertEqual(reset_requested.status_code, 200, reset_requested.text)
        self.assertEqual(len(self.sent_emails), 1)
        reset_token = re.search(
            r"reset_token=([^\s]+)", self.sent_emails[-1][2]
        )
        self.assertIsNotNone(reset_token)
        reset = self.client.post(
            "/api/v1/auth/password-reset/complete",
            json={
                "token": reset_token.group(1),
                "new_password": "a newer secure password 456",
            },
        )
        self.assertEqual(reset.status_code, 200, reset.text)
        self.assertEqual(
            self.client.get("/api/v1/auth/session").status_code,
            401,
        )
        new_password_login = self.client.post(
            "/api/v1/auth/login",
            json={
                "email": "new-member@example.test",
                "password": "a newer secure password 456",
            },
        )
        self.assertEqual(new_password_login.status_code, 200)

        settings.REQUIRE_EMAIL_VERIFICATION = True
        self.assertEqual(
            self.client.get("/api/v1/auth/session").status_code,
            401,
        )
        verification_required = self.client.post(
            "/api/v1/auth/login",
            json={
                "email": "new-member@example.test",
                "password": "a newer secure password 456",
            },
        )
        self.assertEqual(verification_required.status_code, 403)
        with self.sessions() as db:
            new_user = db.query(User).filter_by(
                email="new-member@example.test"
            ).one()
            self.assertFalse(new_user.email_verified)

    def test_existing_unverified_account_can_log_in_without_account_mutation(self):
        settings.REQUIRE_EMAIL_VERIFICATION = False
        settings.ALLOWED_REGISTRATION_EMAIL_DOMAINS = ["example.test"]
        original_password = "existing account password 789"
        with self.sessions() as db:
            user = User(
                email="existing@example.test",
                password_hash=authentication._hash_password(original_password),
                email_verified=False,
            )
            db.add(user)
            db.commit()
            original_hash = user.password_hash

        response = self.client.post(
            "/api/v1/auth/login",
            json={
                "email": "existing@example.test",
                "password": original_password,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get("/api/v1/auth/session").status_code, 200)
        with self.sessions() as db:
            user = db.query(User).filter_by(email="existing@example.test").one()
            self.assertFalse(user.email_verified)
            self.assertEqual(user.password_hash, original_hash)

    def test_verification_disabled_requires_and_enforces_team_domain_allowlist(self):
        settings.REQUIRE_EMAIL_VERIFICATION = False
        settings.ALLOWED_REGISTRATION_EMAIL_DOMAINS = []
        missing_allowlist = self.client.post(
            "/api/v1/auth/register",
            json={
                "email": "member@example.test",
                "password": "a secure password 123",
            },
        )
        self.assertEqual(missing_allowlist.status_code, 503)

        settings.ALLOWED_REGISTRATION_EMAIL_DOMAINS = ["example.test"]
        external_domain = self.client.post(
            "/api/v1/auth/register",
            json={
                "email": "member@outside.test",
                "password": "a secure password 123",
            },
        )
        self.assertEqual(external_domain.status_code, 403)

    def test_cookie_access_is_project_scoped_and_mutations_require_csrf(self):
        email = self._register_and_verify()
        csrf = self._login(email)
        db = self.sessions()
        project = Project(
            name="owned",
            token_hash=auth.hash_api_key(auth.create_api_key()),
            owner_user_id=db.query(User).filter_by(email=email).one().id,
        )
        other = Project(
            name="unowned",
            token_hash=auth.hash_api_key(auth.create_api_key()),
        )
        db.add_all([project, other])
        db.commit()
        project_id = project.id
        other_id = other.id
        db.close()

        owned = self.client.get(
            "/api/v1/experiments", headers={"X-Project-ID": project_id}
        )
        forbidden = self.client.get(
            "/api/v1/experiments", headers={"X-Project-ID": other_id}
        )
        self.assertEqual(owned.status_code, 200)
        self.assertEqual(forbidden.status_code, 404)
        missing_csrf = self.client.delete(
            f"/api/v1/experiments/{'no-experiment'}",
            headers={"Origin": "http://testserver", "X-Project-ID": project_id},
        )
        self.assertEqual(missing_csrf.status_code, 403)
        self.assertTrue(csrf)

    def test_legacy_api_key_remains_valid_and_claim_is_one_time(self):
        email = self._register_and_verify()
        csrf = self._login(email)
        legacy_key = auth.create_api_key()
        db = self.sessions()
        project = Project(
            name="legacy",
            token_hash=auth.hash_api_key(legacy_key),
            owner_user_id=None,
        )
        db.add(project)
        db.commit()
        project_id = project.id
        db.close()

        self.assertEqual(auth.project_for_api_key(legacy_key).id, project_id)
        claim = self.client.post(
            "/api/v1/auth/projects/claim",
            json={"api_key": legacy_key},
            headers={"Origin": "http://testserver", "X-CSRF-Token": csrf},
        )
        self.assertEqual(claim.status_code, 200, claim.text)
        self.assertEqual(claim.json()["project_id"], project_id)
        with self.sessions() as db:
            self.assertEqual(db.query(Project).filter_by(id=project_id).one().owner_user_id, db.query(User).filter_by(email=email).one().id)

    def test_unverified_users_cannot_login_and_reset_tokens_are_single_use(self):
        self.client.post(
            "/api/v1/auth/register",
            json={"email": "new@example.test", "password": "a secure password 123"},
        )
        denied = self.client.post(
            "/api/v1/auth/login",
            json={"email": "new@example.test", "password": "a secure password 123"},
        )
        self.assertEqual(denied.status_code, 403)

        self._register_and_verify("reset@example.test")
        reset = self.client.post(
            "/api/v1/auth/password-reset/request",
            json={"email": "reset@example.test"},
        )
        self.assertEqual(reset.status_code, 200)
        token = re.search(r"reset_token=([^\s]+)", self.sent_emails[-1][2])
        self.assertIsNotNone(token)
        payload = {
            "token": token.group(1),
            "new_password": "an even safer password 123",
        }
        changed = self.client.post(
            "/api/v1/auth/password-reset/complete", json=payload
        )
        replay = self.client.post(
            "/api/v1/auth/password-reset/complete", json=payload
        )
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(replay.status_code, 400)

    def test_api_key_experiment_finish_and_clear_preserve_metadata(self):
        project_key = auth.create_api_key()
        db = self.sessions()
        project = Project(
            id="project-finish-test",
            name="finish test",
            token_hash=auth.hash_api_key(project_key),
        )
        experiment = Experiment(
            id="experiment-finish-test",
            project_id=project.id,
            name="finish test",
            framework="pytorch",
            model_name="Linear",
            status="running",
            config={},
            latest_epoch=-1,
        )
        db.add_all([project, experiment])
        db.commit()
        project_id = project.id
        experiment_id = experiment.id
        experiment_name = experiment.name
        db.close()
        live_state.create_experiment(
            experiment_id, project_id, experiment_name, "pytorch", "Linear"
        )
        headers = {"Authorization": f"Bearer {project_key}"}

        finished = self.client.post(
            f"/api/v1/experiments/{experiment_id}/finish",
            headers=headers,
        )
        self.assertEqual(finished.status_code, 200, finished.text)
        self.assertEqual(finished.json()["status"], "finished")
        cleared = self.client.delete(
            f"/api/v1/experiments/{experiment_id}",
            headers=headers,
        )
        self.assertEqual(cleared.status_code, 200, cleared.text)
        metadata = self.client.get(
            f"/api/v1/experiments/{experiment_id}",
            headers=headers,
        )
        self.assertEqual(metadata.status_code, 200, metadata.text)
        self.assertEqual(metadata.json()["status"], "finished")

    def test_project_credentials_are_one_time_scoped_revocable_and_rotatable(self):
        email = self._register_and_verify()
        csrf = self._login(email)
        mutation_headers = {
            "Origin": "http://testserver",
            "X-CSRF-Token": csrf,
        }

        created = self.client.post(
            "/api/v1/projects",
            json={"name": "Kaggle project"},
            headers=mutation_headers,
        )
        self.assertEqual(created.status_code, 200, created.text)
        project = created.json()
        self.assertEqual(project["name"], "Kaggle project")
        self.assertNotIn("api_key", project)
        with self.sessions() as db:
            stored_project = db.query(Project).filter_by(
                id=project["project_id"]
            ).one()
            self.assertIsNone(stored_project.token_hash)

        issue = self.client.post(
            f"/api/v1/projects/{project['project_id']}/credentials",
            json={"name": "Kaggle Training"},
            headers=mutation_headers,
        )
        self.assertEqual(issue.status_code, 200, issue.text)
        issued = issue.json()
        raw_key = issued["api_key"]
        self.assertTrue(raw_key.startswith("ns_live_"))
        with self.sessions() as db:
            stored = db.query(ProjectAPIKey).filter_by(
                id=issued["credential_id"]
            ).one()
            self.assertEqual(stored.name, "Kaggle Training")
            self.assertEqual(stored.token_hash, auth.hash_api_key(raw_key))
            self.assertNotEqual(stored.token_hash, raw_key)

        listed = self.client.get(
            f"/api/v1/projects/{project['project_id']}/credentials"
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertNotIn("api_key", listed.text)
        self.assertEqual(listed.json()[0]["name"], "Kaggle Training")

        valid = self.client.get(
            "/api/v1/experiments",
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        self.assertEqual(valid.status_code, 200, valid.text)

        rotated = self.client.post(
            f"/api/v1/projects/{project['project_id']}/credentials/"
            f"{issued['credential_id']}/rotate",
            json={"name": "Kaggle Training"},
            headers=mutation_headers,
        )
        self.assertEqual(rotated.status_code, 200, rotated.text)
        replacement = rotated.json()
        self.assertNotEqual(replacement["api_key"], raw_key)
        self.assertEqual(
            self.client.get(
                "/api/v1/experiments",
                headers={"Authorization": f"Bearer {raw_key}"},
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.get(
                "/api/v1/experiments",
                headers={"Authorization": f"Bearer {replacement['api_key']}"},
            ).status_code,
            200,
        )

        revoked = self.client.delete(
            f"/api/v1/projects/{project['project_id']}/credentials/"
            f"{replacement['credential_id']}",
            headers=mutation_headers,
        )
        self.assertEqual(revoked.status_code, 200, revoked.text)
        rejected = self.client.get(
            "/api/v1/experiments",
            headers={"Authorization": f"Bearer {replacement['api_key']}"},
        )
        self.assertEqual(rejected.status_code, 401)

    def test_project_ownership_rename_and_archive_revoke_access(self):
        email = self._register_and_verify()
        csrf = self._login(email)
        mutation_headers = {
            "Origin": "http://testserver",
            "X-CSRF-Token": csrf,
        }
        created = self.client.post(
            "/api/v1/projects",
            json={"name": "Before rename"},
            headers=mutation_headers,
        ).json()
        project_id = created["project_id"]
        issued = self.client.post(
            f"/api/v1/projects/{project_id}/credentials",
            json={"name": "Kaggle Training"},
            headers=mutation_headers,
        ).json()

        renamed = self.client.patch(
            f"/api/v1/projects/{project_id}",
            json={"name": "After rename"},
            headers=mutation_headers,
        )
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(renamed.json()["name"], "After rename")

        archived = self.client.delete(
            f"/api/v1/projects/{project_id}",
            headers=mutation_headers,
        )
        self.assertEqual(archived.status_code, 200, archived.text)
        self.assertEqual(archived.json()["status"], "archived")
        self.assertEqual(self.client.get("/api/v1/projects").json(), [])
        self.assertEqual(
            self.client.get(
                "/api/v1/experiments",
                headers={"Authorization": f"Bearer {issued['api_key']}"},
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.get(
                f"/api/v1/projects/{project_id}/credentials"
            ).status_code,
            404,
        )

    def test_users_cannot_manage_credentials_for_another_users_project(self):
        owner_email = self._register_and_verify("owner@example.test")
        owner_csrf = self._login(owner_email)
        owner_headers = {
            "Origin": "http://testserver",
            "X-CSRF-Token": owner_csrf,
        }
        created = self.client.post(
            "/api/v1/projects",
            json={"name": "Private"},
            headers=owner_headers,
        ).json()
        self.client.post("/api/v1/auth/logout", headers=owner_headers)

        self._register_and_verify("other@example.test")
        other_csrf = self._login("other@example.test")
        other_headers = {
            "Origin": "http://testserver",
            "X-CSRF-Token": other_csrf,
        }
        response = self.client.post(
            f"/api/v1/projects/{created['project_id']}/credentials",
            json={"name": "Unauthorized"},
            headers=other_headers,
        )
        self.assertEqual(response.status_code, 404)

    def test_project_creation_is_rate_limited(self):
        email = self._register_and_verify()
        csrf = self._login(email)
        headers = {
            "Origin": "http://testserver",
            "X-CSRF-Token": csrf,
        }
        for index in range(20):
            response = self.client.post(
                "/api/v1/projects",
                json={"name": f"Project {index}"},
                headers=headers,
            )
            self.assertEqual(response.status_code, 200, response.text)

        rejected = self.client.post(
            "/api/v1/projects",
            json={"name": "Too many"},
            headers=headers,
        )
        self.assertEqual(rejected.status_code, 429)
        self.assertIn("Retry-After", rejected.headers)

    def test_dashboard_websocket_uses_cookie_and_closes_cleanly(self):
        email = self._register_and_verify()
        csrf = self._login(email)
        project_key = auth.create_api_key()
        db = self.sessions()
        user = db.query(User).filter_by(email=email).one()
        project = Project(
            id="project-websocket-test",
            name="WebSocket test",
            token_hash=auth.hash_api_key(project_key),
            owner_user_id=user.id,
        )
        db.add(project)
        db.commit()
        project_id = project.id
        db.close()
        live_state.create_experiment(
            "experiment-websocket-test",
            project_id,
            "WebSocket test",
            "pytorch",
            "Linear",
        )

        self.client.cookies.clear()
        with self.assertRaises(WebSocketDisconnect) as rejected:
            with self.client.websocket_connect(
                f"/api/v1/experiments/experiment-websocket-test/stream"
                f"?project_id={project_id}&api_key={project_key}",
                headers={"Origin": "http://testserver"},
            ):
                pass
        self.assertEqual(rejected.exception.code, 4401)

        self._login(email)
        with self.client.websocket_connect(
            f"/api/v1/experiments/experiment-websocket-test/stream?project_id={project_id}",
            headers={"Origin": "http://testserver"},
        ) as websocket:
            snapshot = websocket.receive_json()
            self.assertEqual(snapshot["type"], "snapshot")
            self.assertEqual(snapshot["experiment_id"], "experiment-websocket-test")

        self.assertTrue(csrf)
        live_state.delete_experiment("experiment-websocket-test", project_id)
