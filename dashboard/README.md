# NeuronScope dashboard

The React dashboard uses the FastAPI backend for account sessions, owned-project
discovery, experiment metadata, five-epoch live snapshots, collapse-event
evidence, and live WebSocket updates. It does not create experiments, store
metrics, or infer network edges.

## Local development

```powershell
Copy-Item .env.example .env.local
# Set VITE_API_PROXY_TARGET to your local backend origin.
npm install
npm run dev
```

`VITE_API_PROXY_TARGET` is used only by the Vite development server and proxies
`/api/v1` HTTP and WebSocket traffic to the backend. Set backend
`DASHBOARD_ORIGINS` and `CORS_ORIGINS` to `http://localhost:5173`; use
`SESSION_COOKIE_SECURE=false` for local HTTP only. `VITE_API_BASE_URL` is an
origin only, without `/api/v1`; leave it empty when a same-origin reverse proxy
forwards API and WebSocket traffic. HTTPS API origins automatically use WSS.
There is deliberately no `VITE_API_KEY`: values prefixed with `VITE_` are
public and must never contain a permanent credential.

Email verification and password-reset delivery require backend SMTP
configuration. With `REQUIRE_EMAIL_VERIFICATION=false`, registration, login,
and SDK device approval do not need SMTP; resend-verification is a no-op, while
password reset still needs SMTP. For local-only development, configure SMTP to
a local test sink when testing verification or password reset, and set
`SESSION_COOKIE_SECURE=false`; do not use that cookie setting in production.

## Backend deployment

Configure these backend environment variables through the deployment secret
manager (never through dashboard build variables):

- `DATABASE_URL`, `SECRET_KEY`
- `DASHBOARD_BASE_URL` and `DASHBOARD_ORIGINS`
- `CORS_ORIGINS`
- `SESSION_COOKIE_SECURE=true` when serving over HTTPS
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`,
  `SMTP_FROM_EMAIL`, and `SMTP_USE_TLS`

Apply the database migration before deploying the authenticated dashboard.
Email verification is required by default and can be temporarily disabled for a
private team deployment with `REQUIRE_EMAIL_VERIFICATION=false` and a non-empty
`ALLOWED_REGISTRATION_EMAIL_DOMAINS` allowlist. The backend refuses to start
without that allowlist when verification is disabled. See the root deployment
guide for the domain-only access trade-off and SMTP requirements.

The dashboard uses short-lived HttpOnly
session cookies, sends the session CSRF token for mutations, and scopes requests
to an owned project with `X-Project-ID`. WebSockets use the session cookie and
the selected project ID; no API key is placed in browser JavaScript or the
WebSocket URL.

Serve the dashboard and API under the same site, preferably via a reverse proxy.
For a separate frontend/API origin, configure CORS and `DASHBOARD_ORIGINS`
exactly, use HTTPS for both, and validate browser cookie behavior for your
deployment's site boundaries. Do not weaken cookie, CSRF, or origin checks to
make cross-site cookies work.

## SDK onboarding

On a trusted workstation, `neuronscope.init()` can open the account's
authorization page, where the signed-in user approves the SDK. Credentials are
stored in the operating system credential manager. If no secure OS credential
store is available, provide `NEURONSCOPE_ENDPOINT`, `NEURONSCOPE_PROJECT_ID`,
and `NEURONSCOPE_API_KEY` explicitly through environment variables.

Kaggle users should store those values in Kaggle Secrets and expose them to the
training process as environment variables. Do not put credentials in notebook
source, output, or checked-in files. A legacy `ns_live_...` key can be claimed
once by a verified account in the authenticated backend flow.

## Behavior and limitations

- The SDK creates/uses the project and experiment; the dashboard also supports
  owned-project creation, rename/archive, and named SDK credential issue,
  rotation, and revocation. Project discovery refreshes every 15 seconds and
  experiment discovery every 5 seconds while the dashboard is open.
- WebSockets carry live metrics/events; reconnects trigger a fresh backend
  snapshot. The dashboard keeps no more than the backend's rolling five-epoch
  view in browser memory.
- Layer membership and neuron IDs come from architecture data. The backend
  provides no edge list, so no network connections are drawn. For very wide
  layers, the graph shows at most 24 neurons per layer; the selector can inspect
  any declared neuron.
- Missing and invalid metrics render as unavailable/gaps; zero remains valid.
  Live data is distinguished from a cached snapshot when disconnected.
- Collapse-event evidence is displayed separately from the rolling live metric
  window. Event timestamps use backend `created_at`; when absent, the UI shows
  the detected epoch and says the timestamp is unavailable.
- **Clear live state** calls `DELETE /api/v1/experiments/{id}`. This clears
  process-local architecture, live metrics, and events, but does not delete
  persistent project or experiment metadata.

## Validate and build

```powershell
npm test
npm run build
npm run preview
```
