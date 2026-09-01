# Flight-recorder capture policy — operator mutation surface

## Overview

The dispatch flight recorder (`docs/decisions/dispatch-flight-recorder.md`)
captures redacted vendor traffic per dispatch, gated by a per-tenant policy and
a per-target override that the resolver
(`meho_backplane/flight_recorder/config.py`) reads on the hot path. The policy
columns (#3212/#3216) and the resolver shipped, but there was **no writable
path** — capture could not be enabled on a deployment without direct DB writes.
This surface (#3272) is the operator-plane mutation path that closes that gap.

It is an **operator-plane** surface: REST + CLI only, gated at the
`tenant_admin` tier. It is deliberately **not** on the 25-tool agent working
surface and has **no MCP tool** — enabling capture is a governance decision, an
operator action, not an agent one. (Simplest correct answer per postulate 5:
REST + CLI, no MCP tool at all.)

## Routes

- `PATCH /api/v1/tenants/flight-recorder-policy`
  (`meho_backplane/api/v1/tenants.py`) — the three per-tenant policy fields.
  **Tenant-scoped to the caller's own tenant** (`operator.tenant_id` from the
  JWT); it accepts no tenant id in the path or body, so a cross-tenant write is
  not expressible. `require_role(TenantRole.TENANT_ADMIN)`. Body
  `TenantFlightRecorderPolicyUpdate` (all fields optional-partial,
  `extra='forbid'`); returns the resolved `TenantFlightRecorderPolicy`.
  - `flight_recorder_enabled` (F1) — Boolean, `NOT NULL`. Absent = unchanged;
    `true`/`false` flips the capture default. Explicit `null` is a 422 (no
    inherit state).
  - `flight_recorder_agent_readable` (F5) — **tri-state**. Absent = unchanged;
    `true`/`false` force the agent-read override; `null` clears it to inherit.
  - `flight_recorder_retention_days` (F4) — nullable, bounded **1..365** (the
    reaper window; matches `Settings.flight_recorder_retention_days_default`).
    Absent = unchanged; an int sets the window; `null` clears to the global
    default.

- `PATCH /api/v1/targets/{name}` (`meho_backplane/api/v1/targets.py`) — the
  per-target override `flight_recorder_capture` rides the **existing** target
  PATCH (and `POST /api/v1/targets` for a seeded value), not a new route.
  **Tri-state** on `TargetUpdate`: absent = unchanged; `true`/`false` force
  capture on/off for the target; explicit `null` clears back to inherit. The
  handler keys off `model_fields_set` (`exclude_unset`), **not** off `None`, so
  the JSON-null-vs-absent distinction is preserved.

## Null-vs-absent (the tri-state discipline)

Both routes lean on Pydantic `exclude_unset`: an omitted key leaves the column
untouched; an explicit JSON `null` is an intentional clear-to-inherit /
clear-to-default. The CLI mirrors this by sending a **sparse** body (only the
flags the operator set) rather than the generated update struct, whose pointer
fields carry no `omitempty` and would marshal a nil to an unintended `null`.

## Audit

Every applied change is folded into the request's `audit_log` row via `audit_*`
contextvars naming field / old / new (governance-relevant, never silent) —
`update_flight_recorder_policy` for the tenant fields,
`_audit_flight_recorder_capture_write` for the target override. Tri-state `None`
is bound as the `inherit` sentinel because the audit-payload builder drops
`None` contextvars (mirroring the CA-pin audit's `""` marker). A no-op / absent
field binds nothing.

## Cache invalidation

The resolver caches per-tenant policy and per-target override for 60s. Each
route calls `invalidate_tenant_policy_cache` / `invalidate_target_override_cache`
on a change so the flip takes effect on the **next dispatch** rather than
waiting out the TTL or a restart (proven by the without-restart tests in
`test_api_v1_tenants_flight_recorder.py`, `test_targets_flight_recorder_capture.py`,
and `test_flight_recorder_config.py`).

## CLI

- `meho tenants flight-recorder-policy set [--enabled] [--agent-readable
  true|false|inherit] [--retention-days N | --clear-retention]`
  (`cli/internal/cmd/tenants/`) — the tenant policy PATCH.
- `meho targets import --update` maps a YAML `flight_recorder_capture` key onto
  the target PATCH (`cli/internal/cmd/targets/import.go`, `knownTopLevel`).
