# Frontend API contract v11

All project identifiers and checkpoint identifiers are opaque. A client must
never construct a checkpoint identifier or send a filesystem path.

## History and branches

- `GET /api/projects/{project_id}/branches` returns `current_branch`,
  `current_checkpoint_id`, and branch items with read-only checkpoint metadata.
- `POST /api/projects/{project_id}/branches` with
  `{ "checkpoint": "checkpoint_...", "name": "revision-name" }` reopens history
  as a new branch. It never overwrites the source branch.
- `POST /api/projects/{project_id}/branches/switch` with
  `{ "checkpoint_id": "checkpoint_..." }` switches the active read pointer to
  an indexed checkpoint. Cross-project and forged identifiers are rejected.

## Timeline and traces

- `GET /api/projects/{project_id}/timeline?after=0&limit=100` returns
  `{items, next_cursor, has_more}`. `after` is the last observed event sequence.
- `GET /api/projects/{project_id}/timeline/events` uses the same arguments and
  emits the exact same records as a finite SSE response. Reconnect with the last
  SSE `id` as `after`; polling is always a valid fallback.
- `GET /api/projects/{project_id}/traces?after=0&limit=100` returns immutable,
  recursively redacted prompt/model call audit records with the same page shape.
- Image generation reports real queued/running/completed/failed events only. The
  API does not claim that image models stream preview frames.

The maximum page size is 500. Negative cursors and invalid limits return 422.

## Runtime configuration

Each runtime revision is immutable. Before a project accepts its first run, a
validated revision may replace the initial branch binding and returns
`APPLIED_BEFORE_START`. After execution begins, a project may move to another
validated revision only at a safe checkpoint, where the service creates a new
branch and keeps the source checkpoint and its runtime/model binding unchanged.

- `IMAGE_AGENT_CONFIG_ROOT` registers `revisions/{revision_id}` directories.
  Each directory contains `manifest.json`, `runtime.yaml`, and
  `model_config.yaml`; path traversal, symlinks, unknown revisions, and digest
  mismatches fail closed. The original fixed-file environment variables remain
  the initial-revision compatibility entry point.
- `POST /api/managed/projects/{project_id}/config-revisions/apply` accepts a
  loopback request authenticated with the Adapter header. The command fixes the
  revision ID, source checkpoint, total configuration hash, effective state,
  and idempotency key. A replay returns the original branch receipt.
- `GET /api/projects/{project_id}/runtime-settings` and
  `POST /api/projects/{project_id}/runtime-settings` are standalone-mode project
  settings endpoints. They expose only the editable runtime allowlist and safe
  model names. A confirmed write creates an immutable project-local revision;
  it updates the unstarted initial binding or creates a safe-checkpoint branch,
  and never updates the process defaults.
- `GET /api/projects/{project_id}/runtime-status` returns only structured
  process health, active-job state, the active revision/branch/config digest,
  pending revision identity, and up to five structured recent exceptions. The
  status view does not derive state by parsing log text.

## Runtime context and navigation

`GET /api/runtime-context` tells the browser whether it is standalone or
Harness-managed, the current project/task/instance identity, bridge protocol
version, navigation mode, and explicit capabilities.

In standalone mode the project directory, creation flow, project switching,
current-task settings, and status page remain available. In managed mode the
document removes the project directory and creation UI, opens the bound project
directly, and rejects `GET /api/projects` with `MANAGED_BY_HARNESS`. Every
project-scoped read is still constrained to the bound managed project.

When `INSTANCE_RUNTIME_SETTINGS_V2=1`, a managed iframe may request only the
runtime-settings get/propose/confirm actions over bridge protocol `1.0`. The
child accepts messages only from the exact parent origin derived from the
document referrer, and the parent supplies a one-use nonce tied to the current
instance. Credentials, Provider URLs, filesystem paths, and Adapter headers do
not enter bridge messages. Managed instances without this capability render
current-task settings read-only.

Provider credentials, Provider URLs, filesystem paths, process controls, and
offline mode are neither editable nor returned by the settings endpoints.
Model calls audit the revision ID, branch ID, and total configuration hash used
for that call.
