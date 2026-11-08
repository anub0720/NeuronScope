# NeuronScope backend authentication setup

The backend requires a database, `SECRET_KEY`, and email delivery for dashboard
account verification and password reset. Configure the following through the
deployment environment or secret manager; do not put production credentials in
the frontend's Vite variables.

```text
DATABASE_URL=postgresql+psycopg://...
SECRET_KEY=<random high-entropy server secret>
DASHBOARD_BASE_URL=https://dashboard.example.com
DASHBOARD_ORIGINS=["https://dashboard.example.com"]
CORS_ORIGINS=["https://dashboard.example.com"]
SESSION_COOKIE_SECURE=true
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=...
SMTP_PASSWORD=...
SMTP_FROM_EMAIL=neuronscope@example.com
SMTP_USE_TLS=true
```

Apply the migrations before serving authenticated routes:

```powershell
python -m alembic upgrade head
```

For local development only, use a separate disposable database and SMTP sink,
set `SESSION_COOKIE_SECURE=false` when using plain HTTP localhost, and configure
the exact local dashboard origin in both origin lists. Keep Secure cookies
enabled in production and serve the frontend/API same-site, preferably through
a reverse proxy.

Dashboard registration requires email verification. Browser sessions are
HttpOnly and expire according to `SESSION_TTL_SECONDS`; unsafe cookie-authenticated
requests require a matching `Origin` and `X-CSRF-Token`. Project API keys remain
supported for SDK calls, but the dashboard must not receive permanent project
keys in its JavaScript bundle.

SDK first-run authorization uses a browser device flow. A verified user approves
the short-lived code, after which the backend creates or reuses an owned project
and returns a project-scoped SDK key over HTTPS. Existing unowned projects can
be claimed through the authenticated dashboard by proving possession of the
legacy key. Existing projects are not automatically assigned to any account.

Set `NEURONSCOPE_ENDPOINT` for SDK runs. For environments without a secure OS
credential manager, including Kaggle, also supply
`NEURONSCOPE_PROJECT_ID` and `NEURONSCOPE_API_KEY` from environment variables
backed by the platform's secret manager.
