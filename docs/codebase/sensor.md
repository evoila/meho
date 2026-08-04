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
  the four guard exceptions (`SensorIdentitySubForbiddenError`,
  `SensorOperationNotFoundError`, `SensorRequiresSafeOperationError`,
  `SensorNameConflictError`), each carrying an `error_code` the transports
  surface verbatim.

## Control flow

**Create** (`POST /api/v1/sensors` → `SensorAdminService.create`):

1. The wire schema validates the cadence union (`interval_seconds`
   5..86400 XOR `cron_expr` + timezone), parses the `assertion` into
   `AssertionSpec`, and caps its serialized size (≤ 8 KiB).
2. **Identity-attribution guard (#2699)**: `identity_sub` is the `sub` the
   runner dispatches — and audit-attributes (`AuditLog.operator_sub` /
   broadcast `principal_sub`) — every evaluation under. The service accepts
   only the `"__sensor__"` sentinel or the creating operator's own
   `created_by_sub`; any other value ⇒ 422 `sensor_identity_sub_forbidden`.
   The check runs first (a pure in-memory ownership check, no DB read) and at
   the **service** choke point — `SensorCreate` (Pydantic) cannot see the
   authenticated operator, so one place covers REST + MCP + CLI. This is
   attribution-only: `identity_sub` selects no credentials (the runner's
   downstream principal token is a separate axis, #2642).
3. The service parses `connector_id` into `(product, version, impl_id)`
   (`operations/_lookup.parse_connector_id`) and resolves the
   `EndpointDescriptor` via `lookup_descriptor` (tenant-scoped, then
   global). No descriptor ⇒ 422 `sensor_operation_not_found`.
4. **Safe-only guard**: `descriptor.safety_level != "safe"` ⇒ 422
   `sensor_requires_safe_operation`. This is a create-time-only guard:
   the dispatch-time policy gate (`operations/dispatcher.dispatch`)
   still runs on every evaluation, but it does **not** re-validate
   `safety_level` — a descriptor re-ingested to a less-safe level keeps
   auto-executing on schedule (`checks/runner.py`, "Dispatch
   identity"). The platform-level half of that trade-off — the
   reporting/triage mitigation — is #2702: a connector re-ingest
   that overwrites a pinned op's `safety_level` surfaces the diff — with
   each affected sensor's id/name/tenant_id, scoped to the ingest's
   tenant for tenant-scoped ingests — on the ingest result's
   `safety_changes` field (REST / job report / MCP) and emits an
   `ingest_safety_class_changed` warning per change, so the operator
   knows which sensors to re-audit (see
   `docs/codebase/spec-ingestion.md`, `IngestionResult`).
5. `create_sensor` inserts the row, materialising `next_fire_at`
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
There is **no** update / pause / resume path — `status` is
server-initialized to `active` at create (clients cannot supply it; a body
carrying `status` is a 422) and transitions to `paused` only via #2505's
runner parking.

## Dependencies

- **#2504** `meho_backplane.checks.assertions` — `AssertionSpec` (embedded
  at the wire) and `CheckState` (the five-state vocabulary). Hard
  dependency, already landed.
- `meho_backplane.scheduler.cron` — `is_valid_cron_expr`, `resolve_timezone`,
  `next_fire_after` (shared with the scheduler).
- `meho_backplane.operations._lookup` — descriptor resolution for the guard.
- Migration `0064_create_sensor.py` (`down_revision="0063"`).

## Known issues / boundaries

- **A Sensor's dispatch identity is not the row's `identity_sub`.** The
  runner attributes the evaluation to `identity_sub` (audit, tenant scope)
  but presents the check-runner *service principal*'s token downstream when
  `CHECK_RUNNER_CLIENT_ID` / `CHECK_RUNNER_CLIENT_SECRET` are configured
  (#2642, `auth/runner_identity.py`). The two are deliberately different:
  MEHO attributes to the Sensor, the credential store authenticates the
  principal. **Unconfigured, the runner presents no token at all** — so a
  Sensor whose target's credentials need an operator-context read evaluates
  `unknown` forever. That is the expected shape on a Vault deploy; on a
  `credentialBackend=gsm` deploy using per-operator WIF it is the failure
  mode `checkRunner.*` exists to fix (see `docs/deploying.md` § GSM).
- The safe-only guard's descriptor read and the insert are in separate
  sessions (a TOCTOU window); acceptable because the dispatch-time policy
  gate is the real boundary.
- **The `identity_sub` ownership guard (#2699) is create-time only.** There
  is no update route, so it covers every new row, but any `sensor` row
  persisted *before* the guard landed with a spoofed `identity_sub` keeps
  dispatching (and audit-attributing) under it — no data migration
  normalises historical rows. Deployments that ran the pre-guard build
  should re-audit existing sensors' `identity_sub`. Dropping the per-row
  knob entirely (always dispatch as `__sensor__`) is the stronger fix but a
  breaking wire+schema change, deferred as a human decision.
- OpenAPI note: `AssertionSpec`'s `Field(gt=0)` bounds (e.g. the freshness
  comparator) are the first numeric `exclusiveMinimum` exposed through the
  API; `cli/api/snapshot-openapi.py` downgrades them to the OpenAPI 3.0
  boolean idiom so oapi-codegen can consume the snapshot.

## References

- Initiative #2416 (binding design), Task #2503, dependency #2504, parent
  goal #221. Runner #2505 (see `docs/codebase/checks-runner.md`, which
  also covers the #2763 evaluation-loop watchdog), dashboard/rollup
  #2506, investigator #2507 build on this storage shape.
- Mould: `ScheduledTrigger` (`db/models.py`), `scheduler/` service/repo/
  schemas, `docs/codebase/scheduler.md`.
