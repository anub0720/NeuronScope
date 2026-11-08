# NeuronScope live API

The backend keeps architecture metadata, the latest five epochs of neuron
metrics, and collapse-event snapshots in process memory. Epoch ingestion does
not write to `metric_batches` or `metric_observations`. Project, experiment,
and architecture metadata continue to use the existing PostgreSQL schema.

SDK experiment HTTP endpoints require a project API key in the `Authorization`
`Bearer` header. Dashboard account and project-management endpoints use the
HttpOnly session cookie; unsafe cookie-authenticated requests also require a
valid CSRF token and trusted `Origin`. The SDK registers experiments and their
architecture; users do not need to create experiments manually. To restore
process-local state after a backend restart, the SDK resumes with the same
`experiment_id`, re-registers architecture, and replays its recent five epochs.

## Dashboard project and credential management

After account registration, email verification, and sign-in, the dashboard can
create projects with `POST /api/v1/projects`, list owned projects with
`GET /api/v1/projects`, rename with `PATCH /api/v1/projects/{project_id}`, and
soft-archive with `DELETE /api/v1/projects/{project_id}`. These routes require
an authenticated user session, CSRF token on mutations, and project ownership.
Archiving revokes all active project credentials and clears process-local
monitoring state; project and experiment metadata remain in the database.

Issue a named SDK credential using
`POST /api/v1/projects/{project_id}/credentials`. Rotate it using
`POST /api/v1/projects/{project_id}/credentials/{credential_id}/rotate`, and
revoke it using `DELETE /api/v1/projects/{project_id}/credentials/{credential_id}`.
Credential listing never returns a raw key. The issue/rotate response returns
the raw `ns_live_...` key once; the database stores only its SHA-256 hash.
Existing legacy project keys remain usable until explicitly revoked or rotated.

## Local setup

From PowerShell:

```powershell
Set-Location backend
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

Configure `DATABASE_URL` and `SECRET_KEY` in the backend `.env` for project and
experiment metadata. Once the API key is resolved into process memory, epoch
and event ingestion use the in-memory state manager and do not open database
sessions.

## Register an experiment architecture

`POST /api/v1/experiments/{experiment_id}/architecture`

```json
{
  "version": 1,
  "framework": "pytorch",
  "model_name": "MLP",
  "classes": {"0": "cat", "1": "dog"},
  "layers": [
    {
      "layer_id": "layer_0",
      "type": "Linear",
      "neuron_count": 2,
      "neuron_ids": ["hidden:0", "hidden:1"]
    }
  ]
}
```

`neuron_ids` is optional, but when supplied its length must equal
`neuron_count`. Without it, neuron IDs are learned from the first epoch that
reports them; each must stay mapped to the same layer and the layer cannot
exceed its declared neuron count. Class keys must be contiguous integer indices
starting at zero. Re-registering the same architecture is idempotent; changing
it after registration is rejected.

## Ingest and read live metrics

`POST /api/v1/experiments/{experiment_id}/epochs`

```json
{
  "epoch": 5,
  "neurons": {
    "hidden:0": {
      "layer_id": "layer_0",
      "activation_mean": 0.72,
      "activation_variance": 0.12,
      "inactive_fraction": 0.08,
      "gradient_mean": 0.004,
      "weight_norm": 0.85,
      "class_contribution": [0.82, null],
      "class_contribution_uncertainty": [0.03, null],
      "class_sample_counts": [120, null],
      "class_validity_mask": [true, false]
    }
  }
}
```

Metric arrays use the registered class index order; use `null` or a false
validity-mask entry for absent measurements. Values must be finite. Epochs must
increase monotonically; retrying one of the most recent five epochs with the
same payload is idempotent, while a conflicting or stale epoch is rejected.
Each neuron's buffer includes explicit missing entries when it was omitted from
an accepted epoch, and is bounded to five epochs.

For an epoch larger than the configured request limit, clients may stage
`POST /api/v1/experiments/{experiment_id}/epochs/{epoch}/chunks` requests with
`chunk_index`, `chunk_count`, and `neurons`, then finalize with
`POST /api/v1/experiments/{experiment_id}/epochs/{epoch}/complete` using
`chunk_count`. Chunks remain process-local and are excluded from snapshots and
streams until every chunk is validated and completion succeeds. Identical
chunk and completion retries are idempotent. Only one incomplete epoch may be
staged per experiment; `finish` rejects an incomplete staged epoch.

`class_contribution_uncertainty` is an optional class-indexed array of standard
errors. An unavailable estimate is `null`, including the one-sample case.
This field is additive; existing atomic epoch clients remain supported.

`GET /api/v1/experiments/{experiment_id}/live` returns architecture, current
epoch, live buffers, and preserved collapse events. The legacy persistent
`POST .../batches` route returns `410 Gone`; `GET .../metrics` is retained as a
live-snapshot alias.

## Collapse events

- `POST /api/v1/experiments/{experiment_id}/events`
- `GET /api/v1/experiments/{experiment_id}/events`
- `GET /api/v1/experiments/{experiment_id}/events/{event_id}`

Event payloads include `event_id`, `neuron_id`, `layer_id`, `event_type`,
`detected_epoch`, and optional `affected_classes`, `evidence`, `rule_version`,
`status`, and `recovery`. If evidence is omitted, the backend copies that
neuron's current buffer. Supplied evidence is validated and copied so buffer
eviction cannot change a stored event. Repeated unresolved events for the same
neuron, type, and class set are deduplicated. Send `status: "recovered"` with
matching neuron/type/classes and a new `event_id` to resolve an active event.
Event capacity is
bounded; when full, ingestion returns `429` without evicting unresolved events.

## WebSocket

Connect to `/api/v1/experiments/{experiment_id}/stream`. The initial message
is a full snapshot; subsequent messages are epoch updates, collapse-event
notifications, or recovery notifications. On queue backpressure, obsolete
updates are replaced by a resynchronization marker and the server sends a fresh
snapshot. SDK clients authenticate HTTP requests with a project API key. The
dashboard WebSocket authenticates with its HttpOnly session cookie and a
project ID. Native clients may use a bearer authorization header; API keys in
WebSocket query parameters are not accepted.

## Finish and clear

- `POST /api/v1/experiments/{experiment_id}/finish` stops epoch/event ingestion.
- `DELETE /api/v1/experiments/{experiment_id}/live` clears live metric buffers.
- `DELETE /api/v1/experiments/{experiment_id}/events` clears event snapshots.
- `DELETE /api/v1/experiments/{experiment_id}` clears all active in-memory state
  and notifies connected clients; it does not delete persistent PostgreSQL
  metadata.

## Limits and deployment

Defaults are configurable with `METRIC_WINDOW_SIZE`, `MAX_ACTIVE_EXPERIMENTS`,
`MAX_SUBSCRIBERS_PER_EXPERIMENT`,
`MAX_NEURONS_PER_EXPERIMENT`, `MAX_COLLAPSE_EVENTS_PER_EXPERIMENT`,
`MAX_EXPERIMENT_MEMORY_BYTES`, `MAX_REQUEST_BYTES`, and
`EXPERIMENT_IDLE_SECONDS`; set `CORS_ORIGINS` to a JSON array of dashboard
origins when deploying. The default metric window is five epochs, each
experiment has a 64 MiB serialized-state estimate limit, the maximum request
is 1 MiB, and idle active state expires after 30 minutes. Use a single Uvicorn
worker: all live state is process-local and is not synchronized across workers
or replicas. A process restart clears
live metrics and collapse snapshots; clients must re-register active experiment
state and resynchronize.
