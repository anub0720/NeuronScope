# SDK/backend compatibility contract

This table was checked against the current FastAPI routers, Pydantic schemas,
authentication dependency, and in-memory state manager. The SDK uses the
existing HTTP protocol plus the additive chunk/completion endpoints documented
below; it introduces no additional infrastructure.

## Account, project ownership, and browser sessions

The browser dashboard uses account sessions rather than project keys. Apply
`backend/migrations/versions/91dc874c2a61_add_user_auth_and_sdk_device_grants.py`
before deploying these routes. The backend requires a configured SMTP sender
for email verification and password-reset links.

| Endpoint | Method | Authentication | Behavior |
|---|---|---|---|
| `/api/v1/auth/register` | POST | None | Creates an unverified account and emails a verification link. |
| `/api/v1/auth/verify-email` | POST | Verification token | Verifies the account; tokens are single-use and expire. |
| `/api/v1/auth/login` | POST | Email and password | Creates a short-lived HttpOnly `ns_session` cookie and returns a CSRF token. |
| `/api/v1/auth/session` | GET | Session cookie | Returns the signed-in user and the stable per-session CSRF token. |
| `/api/v1/auth/logout` | POST | Session cookie + CSRF | Revokes the session and expires the cookie. |
| `/api/v1/auth/password-reset/request` | POST | None | Sends a reset link without revealing whether an account exists. |
| `/api/v1/auth/password-reset/complete` | POST | One-time reset token | Sets a new password and revokes existing sessions. |
| `/api/v1/projects` | GET | Session cookie | Lists only the signed-in user's owned projects. |
| `/api/v1/auth/projects/claim` | POST | Session cookie + CSRF + proof of existing project key | Claims an unowned legacy project once; projects owned by another user cannot be claimed. |
| `/api/v1/auth/device` | POST | None | Starts SDK device authorization and returns a short-lived device/user code pair. |
| `/api/v1/auth/device/approve` | POST | Session cookie + CSRF | Approves a device for an owned or newly provisioned project. |
| `/api/v1/auth/device/token` | POST | Device code | Exchanges an approved device authorization for a project ID and SDK key. |

Unsafe cookie-authenticated requests require both an allowed `Origin` and
`X-CSRF-Token`. Project-scoped dashboard requests also include `X-Project-ID`;
the backend checks that the project belongs to the authenticated user. Set
`DASHBOARD_ORIGINS` to the exact dashboard origin and `CORS_ORIGINS` to the
permitted HTTP origins. The dashboard should be served same-site with the API
so the `SameSite=Lax`, Secure, HttpOnly session cookie remains first-party.

The dashboard WebSocket uses the session cookie and a `project_id` query
parameter, with the HTTP `Origin` checked against `DASHBOARD_ORIGINS`. The
legacy SDK project-key HTTP authentication remains supported. Legacy WebSocket
Project API-key authentication is supported for SDK HTTP requests. Dashboard
WebSockets authenticate with the HttpOnly session cookie and project ID; native
clients may use a bearer authorization header. API keys in WebSocket query
parameters are rejected to prevent credentials leaking through URLs and logs.

SDK first-run provisioning opens the dashboard device-approval page. On a
trusted workstation, the SDK stores returned project credentials in the
operating system credential manager. If no secure OS credential store exists,
users can explicitly provide `NEURONSCOPE_ENDPOINT`, `NEURONSCOPE_PROJECT_ID`,
and `NEURONSCOPE_API_KEY` via environment variables or Kaggle Secrets.

For WebSockets, use the dashboard session cookie with `project_id` or a native
client's bearer authorization header. Any older reference below to an
`api_key` query parameter is obsolete; the current backend rejects it.

| Endpoint | Method | Authentication | Request / response | Errors and behavior |
|---|---|---|---|---|
| Current WebSocket rule | — | Dashboard session cookie plus `project_id`, or native-client bearer header | — | API keys in query parameters are rejected; this rule supersedes older clients' query-key behavior. |
| `/api/v1/experiments` | POST | `Authorization: Bearer <api-key>` | Request: `experiment_id` (optional), `name`, `framework`, `model_name`, optional `seed` and `config`. Response: `experiment_id`, `name`, `status`; the server also returns `duplicate` or `resumed` on those paths. | Existing IDs can resume unfinished metadata into process memory. Finished experiments cannot resume (`409`). Auth is resolved against project metadata. |
| `/api/v1/experiments/{experiment_id}/architecture` | POST | Bearer API key | Request: optional `experiment_id`, `version`, `framework`, `model_name`, contiguous string-keyed `classes`, and `layers` with `layer_id`, `type`, `neuron_count`, optional `neuron_ids`. Response includes `status`, `duplicate`, `architecture_id`, and `layer_count`. | Identical registration is idempotent. A different architecture for an already active experiment is `409`. The backend stores metadata only. |
| `/api/v1/experiments/{experiment_id}/epochs` | POST | Bearer API key | Request: optional `experiment_id`, non-negative `epoch`, and `neurons` keyed by stable neuron ID. Each neuron requires `layer_id`; supported optional values include activation/gradient statistics, weight norm, class contribution/count/validity arrays, sample count, and uncertainty. Response: `status`, `duplicate`, `epoch`. | Class arrays must match registered class count; values must be finite; unknown layers/neuron IDs and out-of-order epochs are rejected. An identical retry in the recent window is idempotent. Metrics are RAM-only. |
| `/api/v1/experiments/{experiment_id}/live` | GET | Bearer API key | No request body. Returns snapshot with architecture, `current_epoch`, rolling `epochs`, `live_neuron_buffers`, and `collapse_events`. | `404` if no active in-memory experiment exists. |
| `/api/v1/experiments/{experiment_id}/events` | POST | Bearer API key | Request: optional `experiment_id`, `event_id`, `neuron_id`, `layer_id`, `event_type`, optional `affected_classes`, `detected_epoch`, optional `evidence`, `rule_version`, `status`, `recovery`. Response: `status`, `duplicate`, `event`. | Duplicate unresolved events are suppressed. Event capacity returns `429`; evidence conflicts with known live measurements return `422`. `status: recovered` resolves an existing event using the same neuron, type, and class set; use a distinct request `event_id`. |
| `/api/v1/experiments/{experiment_id}/events` | GET | Bearer API key | Returns `experiment_id` and the preserved `events` array. | `404` if no active in-memory experiment exists. |
| `/api/v1/experiments/{experiment_id}/events/{event_id}` | GET | Bearer API key | Returns the stored event and independent evidence snapshot. | `404` for an unknown event. |
| `/api/v1/experiments/{experiment_id}/finish` | POST | Bearer API key | No request body. Returns `experiment_id`, `status: finished`, `current_epoch`. | Subsequent epoch/event ingestion is rejected. Does not clear memory. |
| `/api/v1/experiments/{experiment_id}/live` | DELETE | Bearer API key | No request body. Clears live buffers and returns removed-record count. | Clears only process-local live measurements. |
| `/api/v1/experiments/{experiment_id}/events` | DELETE | Bearer API key | No request body. Clears preserved events and returns removed-event count. | Clears only process-local event snapshots. |
| `/api/v1/experiments/{experiment_id}` | DELETE | Bearer API key | No request body. Clears process-local state and returns `status: deleted`. | Persistent experiment metadata is not deleted. |
| `/api/v1/experiments/{experiment_id}/stream` | WebSocket | Bearer header or `api_key` query parameter | First message is a `snapshot`. Incremental messages include `epoch_update`, `collapse_event`, and `collapse_recovered`; on backpressure a fresh snapshot replaces queued stale updates. | Close codes `4401` for invalid/missing key, `4404` for missing active experiment, and `4429` for subscription capacity. SDK uploads use HTTP and do not require a WebSocket. |

## SDK implementation constraints and backend gaps

- Additive endpoints: `POST /api/v1/experiments/{experiment_id}/epochs/{epoch}/chunks`
  stages `chunk_index`, `chunk_count`, and `neurons`; `POST
  /api/v1/experiments/{experiment_id}/epochs/{epoch}/complete` commits only
  when all chunks are present. Staged data remains process-local and hidden
  from snapshots until completion. The existing atomic `/epochs` route remains
  supported.
- The live neuron schema now accepts the optional class-indexed
  `class_contribution_uncertainty` standard-error array; the existing scalar
  `uncertainty` remains unchanged.
- The backend architecture layer schema currently has no metadata field for
  input/output dimensions or graph edges. SDK discovery retains dimensions and
  module paths locally, but only sends the accepted layer fields. Smallest
  backend enhancement: add optional JSON `metadata` to `ArchitectureLayer`,
  include it in architecture equality checks, and persist it to `Layer.metadata_json`.
- The backend rejects changing an architecture after its first active
  registration, even though its request has a `version` field. The SDK
  registers once and reports a clear conflict if a changed model is detected;
  architecture migration requires a backend change that permits versioned
  replacement.
- `finish()` only marks metadata finished and keeps in-memory state. SDK
  `clear()` uses the existing DELETE experiment endpoint; neither operation
  deletes PostgreSQL metadata.
- A new backend process loses active metric and event RAM. SDK recovery uses
  the existing explicit experiment-ID resume, architecture registration,
  epoch POST, and event POST endpoints. No new sync endpoint is required.
- Backend snapshots expose their own five-epoch buffers as a per-neuron list of
  `{epoch, metrics}` entries. Epoch submissions accept sparse neuron maps and
  represent omissions as missing values. Class arrays must keep the registered
  mapping order.
- The backend limits an event evidence `epochs` array to its configured metric
  window and checks supported series against that length. SDK event snapshots
  therefore include at most the backend's configured five-epoch window.

## Supported model and metric boundaries

Architecture discovery supports `Linear` and `Conv1d`, `Conv2d`, and `Conv3d`
modules. It does not infer graph edges from module traversal order. Class-wise
ablation uses classification logits plus integer labels and is opt-in because
its cost grows with the number of selected neurons and probe batches. Gradient
statistics are available only for modules whose forward outputs participate in
autograd. The SDK does not claim distributed-training synchronization; use one
monitor on the process that owns the model and avoid concurrent ranks sending
the same epoch.
