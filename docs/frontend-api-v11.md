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

## Settings

- `GET /api/settings/schema` returns the global `runtime.yaml` defaults without
  requiring an opened project.
- `POST /api/settings/policy` atomically updates the global defaults. When
  `project_id` is supplied it also creates and applies an audited policy branch
  for that currently opened project.
- `GET /api/projects/{project_id}/settings/schema` returns the Pydantic-derived
  field schema, nested definitions, current values, production consumer for each
  setting, and its effect scope.
- `POST /api/projects/{project_id}/policy` validates the same strict schema.
  A change requires `confirmed=true` and a non-empty actor, creates a new audit
  branch, and never mutates the existing branch in place.
