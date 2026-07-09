# Runbook template hydration (read-side re-validation)

**Initiative:** #2286 — G0.30 v0.20.0 closed-loop dogfood hardening
**Task:** #2239

## Overview

A runbook template's `steps` are stored as a JSONB column
(`runbook_templates.steps`). The G12.2 service layer does not trust that
column blindly: every read **re-validates** the stored dicts back through
the Pydantic model `RunbookTemplateBody` before handing them to a caller.
That happens in one sink,
`meho_backplane.runbooks.service._steps_from_storage`, which
`show_template` calls directly and which the run side
(`run_service._load_pinned_template_or_none`) calls to hydrate the
template a run is pinned to.

This document explains the read-side re-validation posture, the
regression that made it dangerous (a schema tightening shipped without a
data migration), and the two-part fix in #2239.

## The read-side re-validation posture

`_steps_from_storage` round-trips the raw `steps` list through
`RunbookTemplateBody(...)` and returns the parsed discriminated-union
step models. The docstring calls this out as deliberate: a row that
somehow reached storage malformed surfaces **at read time** rather than
leaking an unvalidated shape to the caller. It is a fail-closed choice —
the read refuses to serve data that no longer satisfies the current
contract.

The tradeoff of fail-closed-on-read is that the *stored* data and the
*current* validation rules must stay in lockstep. Tightening a field
constraint changes what "valid" means for every already-stored row, not
just future writes. If existing rows are not reconciled to the new rule,
the next read of each one raises `pydantic.ValidationError`.

## The #2122 regression

PR #2122 (Initiative #2117, an ancestor of v0.20.0) tightened both step
variants' `body` field:

```python
body: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
```

on `OperationCallStep` and `ManualStep` (`runbooks/schemas.py`). Correct
going forward — a step with no instructions is not useful — but it
shipped **forward-only**, with no data migration. Any pre-v0.20.0 row
whose step body was empty or whitespace-only (both strip to length 0, so
both are now invalid) became unreadable:

- `GET /api/v1/runbooks/templates/{slug}` returned a bare `text/plain`
  500 (no route handler caught `ValidationError`).
- `meho.runbook.show_template` returned an opaque JSON-RPC `-32603
  "internal error: ValidationError"` (the dispatcher catch-all discarded
  the detail).
- **Wider blast radius:** `list_runs` hydrates every listed run's pinned
  template (`run_service._load_pinned_template_or_none`) with no per-row
  guard, so one poisoned template pinned by one run broke
  `GET /api/v1/runbooks/runs` **tenant-wide** — every run in the tenant
  failed to list.

The error path is `('steps', N, <variant>, 'body')` with
`type=string_too_short`.

## The fix (#2239)

Two parts. The migration is the durable repair; the envelopes make any
residual or future malformed row diagnosable.

### 1. Migration `0054_backfill_empty_runbook_step_bodies`

A pure-data Alembic migration (down_revision `0053`) that rewrites every
stored step body matching the constraint's own invalidity condition
(`body.strip() == ""`) to a non-empty placeholder:

> `(no instructions recorded — authored before the v0.20.0
> non-empty-body requirement)`

One rewrite of `runbook_templates` repairs **all** read sinks at once —
runs re-read the same templates row, so a repaired template hydrates
cleanly through `show_template` *and* `list_runs`. It is tenant-agnostic
(the poisoned rows are operator-authored), idempotent (a repaired row no
longer matches, so a replay is a no-op), and does **not** bump
`edited_at` / `edited_by` (a system repair is not an operator edit). The
`downgrade()` is a documented no-op — the pre-upgrade empty string was
invalid and carries no recoverable state.

The rejected alternative was relaxing the read-side re-validation
(validate-on-write-only). The fail-closed read is intentional; the fix
removes the offending *data*, not the guard.

### 2. Structured error envelopes on `show_template` (REST + MCP)

For a residual row (one that predates the migration on a not-yet-upgraded
deploy) or a future malformed row, the template-show surfaces no longer
leak an opaque 500. Both transports catch the hydration `ValidationError`
and emit a structured envelope built by the single shared builder
`meho_backplane.runbooks.hydration_errors.build_template_body_validation_detail`
(one builder so REST and MCP can't drift — the same posture the
connector-ingest envelopes use; see `docs/codebase/error-message-shape.md`):

- **REST** — `HTTPException(500, detail={...})` with the envelope under
  `detail`, declared in OpenAPI on the `GET /{slug}` route so the
  generated CLI / SDK pick it up.
- **MCP** — `McpInternalError` (`-32603`) with the envelope on the
  JSON-RPC `error.data` member (the #1918 precedent), replacing the
  dispatcher catch-all's opaque `internal error: ValidationError`.

The envelope shape:

```json
{
  "error": "template_body_validation_failed",
  "slug": "cert-rotate",
  "version": 2,
  "errors": [
    {"type": "string_too_short", "loc": ["steps", 0, "manual", "body"], "msg": "String should have at least 1 character"}
  ],
  "message": "template_body_validation_failed: stored runbook template 'cert-rotate' v2 has step content that no longer satisfies the template schema ... Apply Alembic migration 0054 to backfill legacy rows ... See docs/codebase/runbook-template-hydration.md."
}
```

`slug` / `version` are the caller's own coordinates (not infrastructure
topology), so echoing them respects the info-leak boundary. On the
operator path the envelope surfaces even for a caller without a completed
run against the (corrupt) row: the row genuinely exists and is genuinely
broken, and a corrupt row is not a clean enumeration oracle (normal
templates return the opacity-floor 403; only the rare corrupt one 500s).
This matches the pre-#2239 bare-500 behaviour on that path — the fix
makes the 500 *structured*, it does not add a new existence channel.

## Out of scope

- Relaxing `min_length=1` on write — correct going forward.
- Run-side (`list_runs` / `get_current_step`) structured envelopes beyond
  what the migration moots — a future malformed row surfaces via the
  template-show envelope first (minimum diff).
- Friendly UI rendering of the (post-migration moot) detail/editor path.

## Dependencies

- `backend/src/meho_backplane/runbooks/service.py` —
  `_steps_from_storage`, the read-side re-validation sink.
- `backend/src/meho_backplane/runbooks/schemas.py` — the
  `StringConstraints(strip_whitespace=True, min_length=1)` on
  `OperationCallStep.body` / `ManualStep.body` (#2122).
- `backend/src/meho_backplane/runbooks/hydration_errors.py` — the shared
  structured-envelope builder.
- `backend/src/meho_backplane/api/v1/runbook_templates.py` — the REST
  `show_template` handlers + the OpenAPI `_SHOW_RESPONSES` 500 declaration.
- `backend/src/meho_backplane/mcp/tools/runbooks.py` — the MCP
  `_show_template_handler` + its `McpInternalError` mapping.
- `backend/alembic/versions/0054_backfill_empty_runbook_step_bodies.py` —
  the data repair.
- `backend/src/meho_backplane/runbooks/run_service.py` —
  `_load_pinned_template_or_none`, the run-side hydration sink whose
  tenant-wide `list_runs` break the migration repairs.

## References

- Regression origin: PR #2122 (Initiative #2117 D-batch).
- Structured-error convention: `docs/codebase/error-message-shape.md`;
  cross-transport shared-builder precedent G0.9.1-T5 #777; MCP
  `McpInternalError` precedent #1918.
- Migration mold: `docs/codebase/migrations.md`; backfill-rewrite
  references `0011` / `0038` / `0052`.
