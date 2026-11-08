# NeuronScope first-time deployment and publishing guide

This guide is intended to be tracked at the repository root and is not included
in the SDK distribution.

This guide covers deploying the FastAPI backend and React dashboard, publishing
the Python SDK to PyPI, and setting up a first Kaggle notebook. Provider-specific
button names vary; the required services and configuration are described below.

## 1. Before you deploy

Have the following ready:

- A backend host that can run a persistent ASGI process, connect to PostgreSQL,
  and support HTTPS and WebSocket upgrades.
- A managed PostgreSQL database with TLS enabled and backups configured.
- A static frontend host, or a reverse proxy that can serve the dashboard and
  route API/WebSocket traffic to the backend.
- A verified domain, such as `neuronscope.example.com`. Prefer serving the
  dashboard and API under the same site, for example:
  - Dashboard: `https://neuronscope.example.com`
  - API: `https://neuronscope.example.com/api/v1/...`
- An SMTP account that can send account-verification and password-reset
  messages. SDK device approval happens in the authenticated dashboard.
- A verified account on PyPI before publishing the SDK.
- A Kaggle account with permission to create notebook Secrets.

Use separate staging and production databases, domains, email credentials, and
server secrets. Do not copy real user data into staging.

## 2. Deploy the backend

### 2.1 Provision the database

Create a PostgreSQL database and a dedicated application user with only the
permissions needed by NeuronScope. Require TLS for database connections and
enable managed backups. The application uses SQLAlchemy with Psycopg 3.

Set `DATABASE_URL` in the backend host's secret/environment configuration. Use
the SQLAlchemy Psycopg URL form:

```text
postgresql+psycopg://USER:PASSWORD@HOST:5432/DATABASE?sslmode=require
```

URL-encode special characters in the username or password. Do not put this URL
in source control, frontend variables, a notebook, or a public issue.

### 2.2 Configure backend environment variables

Set these on the backend service using its secret manager. Do not commit a
production `.env` file.

| Variable | Required value |
|---|---|
| `DATABASE_URL` | PostgreSQL URL as described above |
| `SECRET_KEY` | A new, random, high-entropy secret, unique to this deployment |
| `DASHBOARD_BASE_URL` | Public dashboard origin, e.g. `https://neuronscope.example.com` |
| `DASHBOARD_ORIGINS` | JSON array of exact trusted dashboard origins |
| `CORS_ORIGINS` | JSON array of exact browser origins allowed by CORS |
| `SESSION_COOKIE_SECURE` | `true` in production |
| `SMTP_HOST` | SMTP server hostname |
| `SMTP_PORT` | SMTP server port, commonly `587` |
| `SMTP_USERNAME` | SMTP account name |
| `SMTP_PASSWORD` | SMTP credential or provider token |
| `SMTP_FROM_EMAIL` | Verified sender address |
| `SMTP_USE_TLS` | `true` when the SMTP provider requires TLS |

For a same-origin dashboard/API deployment, both origin variables should contain
the dashboard's exact HTTPS origin, with no trailing slash:

```text
DASHBOARD_BASE_URL=https://neuronscope.example.com
DASHBOARD_ORIGINS=["https://neuronscope.example.com"]
CORS_ORIGINS=["https://neuronscope.example.com"]
SESSION_COOKIE_SECURE=true
```

The origin variables must be valid JSON arrays, not comma-separated lists.
Never use `*` for credentialed browser access. Add every additional trusted
origin explicitly and only when necessary.

Generate a server secret locally and put it directly into the deployment
secret manager; do not check it into the repository:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Keep the secret value private. Changing `SECRET_KEY` invalidates the
server-derived CSRF tokens, so plan rotations and test them.

Other supported settings have defaults in `backend/app/config.py`. In
particular, `METRIC_WINDOW_SIZE` defaults to 5; request and memory limits are
bounded. Only adjust those limits after sizing the service and testing
representative models.

### 2.3 Build and migrate

Use Python 3.10 or newer. From a clean checkout, install the backend
requirements and run the database migration as a one-off release step, using
the same environment variables and release image as the backend:

```powershell
Set-Location backend
python -m pip install -r requirements.txt
python -m alembic upgrade head
```

Run migrations before deploying code that depends on the new tables. Back up
the database before production migrations and check the migration output for
errors.

### 2.4 Start the API

Run the app from the `backend` directory:

```powershell
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

Configure the host to keep **one backend instance and one Uvicorn worker**.
Architecture state, the five-epoch metric window, and collapse-event evidence
are process-local RAM state. Multiple workers or replicas do not share that
state, so users can see inconsistent live data. A process restart clears live
state; project and experiment metadata remain in PostgreSQL, and a running SDK
can re-register/replay bounded recent state.

Do not configure the platform to run Alembic automatically in every worker's
startup command. Run it once per release instead.

### 2.5 Configure ingress and verify the backend

Terminate TLS at the host's ingress or reverse proxy. Route HTTP API requests
under `/api/v1` to the backend and enable WebSocket upgrade/proxying for
`/api/v1/experiments/{experiment_id}/stream`. Preserve the request's `Host`,
`Origin`, and cookie headers. Set suitable proxy timeouts for long-lived
WebSockets.

After deployment, verify:

1. `GET https://YOUR_HOST/` reports the service as running.
2. `GET https://YOUR_HOST/health` reports a healthy database connection.
3. HTTPS is valid and redirects/HTTP behavior match your policy.
4. The deployed service can send an account-verification email.
5. A browser WebSocket can stay connected through the proxy.
6. Backend logs contain no passwords, SMTP credentials, session cookies, or
   project API keys.

The backend exposes its API documentation through FastAPI's `/docs` route.
Decide whether that route should be publicly available in your deployment.

## 3. Deploy the dashboard

### 3.1 Configure API routing

The recommended setup is a same-site deployment:

- Serve the dashboard at `https://neuronscope.example.com`.
- Reverse-proxy `/api/v1` HTTP requests and WebSockets to the backend.
- Build with `VITE_API_BASE_URL` empty so the browser uses the same origin.

The dashboard's production build variables are public JavaScript values.
**Never set `VITE_API_KEY`, `SECRET_KEY`, SMTP credentials, database URLs, or
project API keys.** There is no dashboard API-key variable. The browser uses
HttpOnly session cookies and CSRF protection.

For local development only, `VITE_API_PROXY_TARGET` configures the Vite dev
proxy; it is not the production backend setting. For a separate API origin,
`VITE_API_BASE_URL` may be set to the API origin (without `/api/v1`), but both
sites must use HTTPS and cookie/CORS behavior must be tested in real browsers.
Prefer a same-site reverse proxy rather than weakening cookie or origin checks.

### 3.2 Build and publish static assets

Use Node.js supported by the selected Vite version and the checked-in lockfile:

```powershell
Set-Location dashboard
npm ci
npm test
npm run build
```

Publish the contents of `dashboard/dist` to the static host. Configure SPA
fallback routing to `index.html`, and configure the same-origin API and
WebSocket proxy described above. Set `VITE_API_BASE_URL` at build time if using
a separate API origin; rebuilding is required when changing that public URL.

After deployment, test sign-up, email verification, sign-in/sign-out, project
creation and rename/archive, named credential issue/rotation/revocation,
experiment discovery, WebSocket status, browser refresh, and live state cleanup.
The raw project API key is returned only when a credential is issued or
rotated; it is stored hashed in the database and is not recoverable later.
Confirm that the browser bundle never contains a permanent API key.

## 4. Set up the first account and project

1. Open the public dashboard over HTTPS.
2. Create an account and verify the email using the configured SMTP delivery.
3. Sign in. Dashboard sessions are short-lived, HttpOnly cookies; mutations
   require CSRF and a trusted dashboard origin.
4. Choose one onboarding path:

   **Local workstation:** install the SDK from the checkout while developing:

   ```powershell
   python -m pip install -e .\sdk
   ```

   Set `NEURONSCOPE_ENDPOINT` to the public backend origin, then run
   `neuronscope.init()` in a training script. On first use, the SDK starts a
   short-lived device authorization, opens the dashboard approval page, and
   prints a verification code (never the API key). Sign in and approve it.
   The backend creates or reuses the user's project and returns a project ID
   and SDK key over HTTPS. On supported operating systems the SDK stores them
   in the secure OS credential manager; subsequent runs reuse them. Pass
   `project_id` when selecting a particular project.

   **Dashboard-issued credentials:** create a project from the dashboard, open
   its credential manager, and issue a named SDK credential. Copy the raw key
   immediately; it is shown only in that response. Use this path for Kaggle or
   any machine without a supported secure OS credential manager. The project
   ID, key, and backend endpoint can be shared to that machine only through a
   trusted secret manager.

5. In either path, the SDK automatically registers its experiment and
   architecture before sending metrics. The dashboard discovers the project
   and experiment; do not manually create experiments through API calls.

For an existing legacy project, sign in to the dashboard and use its legacy
project-claim flow with proof of the existing project key. Claiming is explicit;
unowned legacy projects are not automatically assigned to a user.

## 5. Publish the SDK to PyPI

Publishing makes `pip install neuronscope` available in Kaggle and other
Python environments. Confirm that you are authorized to publish the `neuronscope`
distribution name before attempting the release.

### 5.1 Prepare a release

1. Run the SDK tests from the SDK directory:

   ```powershell
   Set-Location sdk
   python -m pip install -e ".[test]"
   python -m pytest
   ```

2. Review and update `sdk/pyproject.toml` before each release:
   - Increment `[project].version`; PyPI does not allow overwriting a released
     version.
   - Verify `requires-python` and runtime dependency versions.
   - Add the project's approved license and project URLs before publishing.
     Do not claim a license until the project owner has selected one.
3. Build and inspect the distribution:

   ```powershell
   python -m pip install --upgrade build twine
   python -m build
   python -m twine check dist/*
   ```

4. Inspect the generated wheel and source archive. Confirm that they contain
   the `neuronscope` package and metadata, not `.env` files, API keys, test
   credentials, or local metric files.
5. Publish a test release to TestPyPI first if the package configuration or
   publishing process is new.

### 5.2 Upload securely

Prefer PyPI Trusted Publishing from a protected release workflow if CI is
configured. Otherwise create a PyPI project-scoped API token and enter it
through a trusted credential prompt/secret manager. Do not commit a token or
place it in a command that is saved in shell history.

Manual upload, with credentials supplied by Twine's secure prompt or protected
environment:

```powershell
python -m twine upload dist/*
```

After publishing, install the exact release in a clean environment and check
`python -m pip show neuronscope`. Treat the published version as immutable;
fix a bad release by publishing a new version.

The SDK declares `torch>=2.0` as a runtime dependency. Validate installation
behavior against the intended Kaggle Python/PyTorch image before announcing a
release, since PyTorch wheels are large and platform-specific.

## 6. Kaggle notebook setup

### 6.1 Add Kaggle Secrets

In the Kaggle notebook settings, add these Secrets and grant the notebook
permission to access them:

| Secret name | Value |
|---|---|
| `NEURONSCOPE_ENDPOINT` | Public HTTPS backend origin |
| `NEURONSCOPE_PROJECT_ID` | ID of the project to use |
| `NEURONSCOPE_API_KEY` | Project-scoped SDK key |

Kaggle does not provide the desktop OS credential manager the SDK uses for
first-run device authorization. Sign in to the dashboard, create or select a
project, and issue a named SDK credential in that project's credential manager.
The dashboard returns the raw key only once. Store it as a Kaggle Secret along
with the project ID and endpoint. Do not put these values in notebook source,
outputs, Git, a public dataset, or a shared notebook. Do not print them for
debugging. Existing valid `ns_live_...` project keys remain supported and can
be managed as legacy credentials.

### 6.2 Install and initialize

Once the package is published and credentials are available as Kaggle Secrets:

```python
%pip install -q neuronscope
```

Load Secrets into the process environment without displaying their values:

```python
import os
from kaggle_secrets import UserSecretsClient

secrets_client = UserSecretsClient()
for name in (
    "NEURONSCOPE_ENDPOINT",
    "NEURONSCOPE_PROJECT_ID",
    "NEURONSCOPE_API_KEY",
):
    os.environ[name] = secrets_client.get_secret(name)
```

Initialize the SDK with the model, class mapping, and experiment name:

```python
import neuronscope

monitor = neuronscope.init(
    model=model,
    experiment_name="kaggle-experiment",
    class_mapping={0: "class-a", 1: "class-b"},
)

try:
    for epoch in range(num_epochs):
        train_one_epoch(model, train_loader)
        monitor.log_epoch(epoch=epoch, probe_loader=probe_loader)
finally:
    monitor.finish()
```

The SDK reads endpoint and project credentials from the environment, registers
the experiment and model architecture, then streams bounded live data. Check
that the public endpoint is reachable from a Kaggle session before a long
training run. If package installation attempts to replace the Kaggle PyTorch
installation, stop and validate dependency resolution in a disposable notebook
before proceeding; do not blindly upgrade PyTorch in an active training
environment.

## 7. First-time user checklist

- [ ] Dashboard and backend use HTTPS with valid certificates.
- [ ] PostgreSQL is backed up and `alembic upgrade head` completed.
- [ ] SMTP delivery works for account verification and password reset.
- [ ] Backend has one instance and one worker.
- [ ] CORS and dashboard origins list only the exact trusted origins.
- [ ] `SESSION_COOKIE_SECURE=true` is enabled in production.
- [ ] Dashboard bundle contains only public configuration; no permanent secrets.
- [ ] A user can sign up, verify email, sign in, and discover owned projects.
- [ ] SDK device authorization succeeds on a supported workstation and stores
      credentials in its OS credential manager.
- [ ] The SDK registers an experiment and the dashboard discovers it.
- [ ] Live metric windows and collapse-event evidence work over WebSockets.
- [ ] Dashboard users can create projects and issue, rotate, and revoke named
      SDK credentials; raw keys are shown only on issue/rotation.
- [ ] A Kaggle notebook retrieves the endpoint, project ID, and key from Kaggle
      Secrets and the SDK validates them before initialization.
- [ ] SDK wheel/source distribution is reviewed and published at a new version.
- [ ] A Kaggle notebook retrieves secrets from Kaggle Secrets and never echoes
      them.
- [ ] The deployment has a tested restart/recovery procedure. Backend restarts
      clear live in-memory metrics/events; they do not delete project metadata.

## 8. Operational limits and incident basics

- Metrics and collapse-event snapshots are in memory, not persisted as a
  permanent time series. Only project, account, and experiment metadata are
  stored in PostgreSQL.
- Backend restarts and idle-state expiry clear the active live view. A running
  SDK can reconnect and replay bounded recent state; old live metrics cannot be
  recovered from PostgreSQL.
- Keep PostgreSQL backups for metadata, restrict database access, and monitor
  database availability, disk, SMTP delivery, backend memory, restart counts,
  and WebSocket disconnects.
- Revoke or rotate project keys if they are exposed. Never put keys in frontend
  build variables, URLs, logs, notebook output, or source control.
- Test upgrades in staging with a disposable project before production. Run
  migrations once and verify `/health`, account email flows, SDK registration,
  dashboard discovery, WebSocket updates, and cleanup.
- If scaling beyond one backend process/instance is required, first replace or
  coordinate the process-local live-state architecture; adding replicas alone
  will not provide reliable shared live monitoring.
