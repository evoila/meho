# Sensor entity + registry (deterministic check layer)

## Overview

A **Sensor** is the first persisted entity of the deterministic check
layer (Initiative #2416, Task #2503). One `sensor` row pins an
`(op + args + assertion + cadence + severity)` tuple that a runner (#2505)
evaluates on a schedule and a Dashboard (#2506) rolls up. The entity is
modelled on `ScheduledTrigger` (`db/models.py`) — the durable-row mould —
but is a deliberately separate table: `ScheduledTrigger.agent_definition_id`
is `NOT NULL` with a real FK, so a trigger row structurally cannot carry an
op-based check.

The layer is deliberately minimal: a Sensor stores a **bounded** assertion
(one select stage feeding one typed comparator, validated by #2504's
`AssertionSpec`), never a free-form assertion language. Storage-only — the
model carries no transition logic; the admin service owns create/list/get/
delete, the runner (#2505) owns claim/advance/park and the result write.

## Key types

- `meho_backplane.db.models.Sensor` — the ORM row. 26 columns: identity
  (`connector_id` + `op_id`), `params`/`target`, `assertion` (JSON), the
  cadence union, `severity`/`for_seconds`, the latest-result projection,
  and `status`/`status_reason`.
- `SensorCadenceKind` (`interval` | `cron`), `SensorStatus`
  (`active` | `paused`), `SensorSeverity` (`degraded` | `critical`) —
  closed StrEnums with DB `CHECK`s. The five-state `last_state` vocabulary
  is **not** re-declared here — it is #2504's
  `meho_backplane.checks.assertions.CheckState`
  (`ok`/`degraded`/`critical`/`unknown`/`skip`); `ck_sensor_last_state` is
  populated from `CheckState`'s members and drift-guarded against them in
  `tests/test_db_sensor.py`.
- `meho_backplane.checks.schemas` — `SensorCreate` (frozen, `extra="forbid"`;
  the `assertion` field is typed with `AssertionSpec`, so a bad select path
  or comparator is a 422 at the wire), `SensorRead`, `SensorListResponse`.
- `meho_backplane.checks.repository` — `create_sensor` (materialises
  `next_fire_at`) and `record_sensor_result` (the one named projection
  write path).
- `meho_backplane.checks.service.SensorAdminService` — tenant-scoped CRUD +
  the three guard exceptions (`SensorOperationNotFoundError`,
  `SensorRequiresSafeOperationError`, `SensorNameConflictError`), each
  carrying an `error_code` the transports surface verbatim.

## Control flow

**Create** (`POST /api/v1/sensors` → `SensorAdminService.create`):

1. The wire schema validates the cadence union (`interval_seconds`
   5..86400 XOR `cron_expr` + timezone), parses the `assertion` into
   `AssertionSpec`, and caps its serialized size (≤ 8 KiB).
2. The service parses `connector_id` into `(product, version, impl_id)`
   (`operations/_lookup.parse_connector_id`) and resolves the
   `EndpointDescriptor` via `lookup_descriptor` (tenant-scoped, then
   global). No descriptor ⇒ 422 `sensor_operation_not_found`.
3. **Safe-only guard**: `descriptor.safety_level != "safe"` ⇒ 422
   `sensor_requires_safe_operation`. This is a create-time honesty guard,
   not the security boundary — the dispatch-time policy gate
   (`operations/dispatcher.dispatch`) still runs on every evaluation, so a
   descriptor re-ingested harder later fails closed at dispatch.
4. `create_sensor` inserts the row, materialising `next_fire_at`
   (`now + interval_seconds` for interval; `next_fire_after(cron_expr, …)`
   for cron) so #2505's claim query (`status='active' AND next_fire_at <= now`)
   is uniform across kinds. A duplicate `(tenant_id, name)` ⇒ 409
   `sensor_name_conflict`.

**Result recording** (`record_sensor_result`, called by #2505/#2507/#2415-T3):
updates `last_state`/`last_value`/`last_evidence`/`last_evaluated_at` on
every call, bumps `state_since` **only** when the state changes, and
returns whether it changed. There is no results history table — the
projection is the single source of current state (Decision D).

**Delete** is a hard `DELETE` (no tombstone) — a sensor carries no
fire-history the audit trail needs post-delete.

Surfaces: REST (`api/v1/sensors.py`, registered in `main.py`), MCP
(`mcp/tools/sensors.py`, auto-loaded), Go CLI (`cli/internal/cmd/sensor/`).
There is **no** update / pause / resume path — `status` is set-at-create
-only and transitions to `paused` only via #2505's parking.

## Dependencies

- **#2504** `meho_backplane.checks.assertions` — `AssertionSpec` (embedded
  at the wire) and `CheckState` (the five-state vocabulary). Hard
  dependency, already landed.
- `meho_backplane.scheduler.cron` — `is_valid_cron_expr`, `resolve_timezone`,
  `next_fire_after` (shared with the scheduler).
- `meho_backplane.operations._lookup` — descriptor resolution for the guard.
- Migration `0064_create_sensor.py` (`down_revision="0063"`).

## Known issues / boundaries

- The safe-only guard's descriptor read and the insert are in separate
  sessions (a TOCTOU window); acceptable because the dispatch-time policy
  gate is the real boundary.
- OpenAPI note: `AssertionSpec`'s `Field(gt=0)` bounds (e.g. the freshness
  comparator) are the first numeric `exclusiveMinimum` exposed through the
  API; `cli/api/snapshot-openapi.py` downgrades them to the OpenAPI 3.0
  boolean idiom so oapi-codegen can consume the snapshot.

## References

- Initiative #2416 (binding design), Task #2503, dependency #2504, parent
  goal #221. Runner #2505, dashboard/rollup #2506, investigator #2507
  build on this storage shape.
- Mould: `ScheduledTrigger` (`db/models.py`), `scheduler/` service/repo/
  schemas, `docs/codebase/scheduler.md`.
