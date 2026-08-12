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

Op-specific probe recipes live with their ops. One worth naming here:
health-checking a **strict-vhost appliance** (a service that vhost-routes
and 404s unless the request's `Host:` matches — e.g. VCFA behind a
NAT-alias IP before DNS exists). Pin a `net.http_probe` Sensor with the
`host_header` param so the probe dials the IP (what the allowlist gates)
yet sends the virtual host as the `Host:` header and TLS SNI; otherwise a
by-IP probe reads a misleading 404 / `tls_error` whether the service is
healthy or wedged. See `docs/codebase/connectors-net-diagnostics.md`
§ *Vhost-routed probes by IP*.

## Key types

- `meho_backplane.db.models.Sensor` — the ORM row. 30 columns: identity
  (`connector_id` + `op_id`), `params`/`target`, `assertion` (JSON), the
  cadence union, `severity`/`for_seconds`, the confirmation knobs
  `retry_times`/`retry_backoff_seconds` (#2799, create-only like every
  other sensor knob), the latest-result projection (including the
  soft-state window `pending_state`/`pending_count`), and
  `status`/`status_reason`. `pending_state`'s CHECK admits `CheckState`
  minus `skip` (`skip` is a rollup-side derivation, never an evaluation
  outcome); `_SENSOR_PENDING_STATES` derives from `CheckState` and is
  drift-guarded in `tests/test_db_sensor.py` against migration `0070`'s
  frozen literal.
- `SensorCadenceKind` (`interval` | `cron`), `SensorStatus`
  (`active` | `paused`), `SensorSeverity` (`degraded` | `critical`) —
  closed StrEnums with DB `CHECK`s. The five-state `last_state` vocabulary
  is **not** re-declared here — it is #2504's
  `meho_backplane.checks.assertions.CheckState`
  (`ok`/`degraded`/`critical`/`unknown`/`skip`); `ck_sensor_last_state` is
  populated from `CheckState`'s members and drift-guarded against them in
  `tests/test_db_sensor.py`.
- `meho_backplane.db.models.SensorResult` — the append-only per-tick evidence
  row (`sensor_results`, #2756); see **Per-tick evidence history**.
- `meho_backplane.checks.schemas` — `SensorCreate` (frozen, `extra="forbid"`;
  the `assertion` field is typed with `AssertionSpec`, so a bad select path
  or comparator is a 422 at the wire), `SensorRead`, `SensorListResponse`,
  plus the trend-query trio `SensorResultRead` / `SensorResultListResponse`
  (`{items, next_cursor}`) / `SensorResultsQuery` (frozen, `extra="forbid"`).
- `meho_backplane.checks.repository` — `create_sensor` (materialises
  `next_fire_at`), `record_sensor_result` (the one named projection write
  path, which also appends the history row when `record_history=True`), and
  `list_sensor_results` (the keyset trend read).
- `meho_backplane.checks.service.SensorAdminService` — tenant-scoped CRUD +
  `list_results` (the trend query) + the guard exceptions
  (`SensorIdentitySubForbiddenError`, `SensorOperationNotFoundError`,
  `SensorRequiresSafeOperationError`, `SensorNameConflictError`,
  `SensorResultsCursorError`), each carrying an `error_code` the transports
  surface verbatim.
- `meho_backplane.checks.evidence_retention` — the lifespan-owned retention
  prune sweeper for `sensor_results` (#2756).

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
updates `last_value`/`last_evidence`/`last_evaluated_at` on every
accepted call, and commits `last_state`/`state_since` through the
**confirmation gate** (#2799, Nagios soft/hard states):

- `retry_times = 0` (default): every differing reading commits
  immediately — the pre-#2799 behaviour, bit for bit.
- `retry_times > 0`: a reading differing from the committed
  `last_state` opens (or advances) the soft-state window —
  `pending_state`/`pending_count` on the row. Same candidate again →
  count increments; `pending_count > retry_times` → commit
  (`last_state` flips, `state_since` = the commit-time `evaluated_at`)
  and the window clears. A different candidate mid-window (escalation)
  restarts the count; a reading equal to the committed state clears the
  window without committing. Symmetric in both directions (recovery to
  `ok` is confirmed too — deliberately unlike Nagios' immediate hard
  recovery, because the observed flap's second nuisance mail was a
  flapping all-clear); `unknown` participates like any state.

The gate lives in the repository function — not the runner task — so
runner-local and satellite-gateway batch-posted results are confirmed
identically. Returns `True` iff the *committed* state changed. A
monotonicity guard ignores a result whose `evaluated_at` is not strictly
newer than the row's last (a stale / reordered / duplicate persist),
leaving the projection untouched. Downstream, `state_since` marks the
*confirmed* transition instant, so a `for_seconds` hold measures confirmed
time on top of confirmation — count-based commit gate first, duration-based
hold second, two independent layers.

The projection (now including the soft-state window) is the current-state
fast path — but it is **no longer the only** record. Task #2503 originally
shipped it as "not a results table" (Decision D: a history table would
demand retention/pruning, speculative until asked for; #2799 annotated that
the consecutive-confirmation counter is carried as projection columns, the
#2327 `skip_count` precedent, not derived from history). Task #2756
(Initiative #2780) supersedes the "no history table" half: a post-incident
review needed "when did this first flap / how fast is it filling", and every
tick overwriting the projection had discarded exactly the history the runner
already computed. So `record_sensor_result` now **also** appends a per-tick
`sensor_results` row (see **Per-tick evidence history** below) **in the same
transaction** as the projection update, gated on retention being enabled —
history and projection can never diverge. The row records the *observed*
outcome of each evaluation (committed, soft/pending, or re-confirmed), so a
flapping reading that never commits still lands in history.

### Per-tick evidence history (#2756)

`meho_backplane.db.models.SensorResult` (`sensor_results` table, migration
`0071`) is an append-only `(sensor_id, evaluated_at, state, value, evidence,
reason)` row per non-stale evaluation. `sensor_id` is a real FK with
**`ON DELETE CASCADE`** — deleting a Sensor drops its history. Because the
monotonicity guard makes `evaluated_at` strictly increasing per sensor, it is
a total order the trend query's keyset cursor rides with no tiebreaker.

- **Retention is deploy-level**: `CHECKS_EVIDENCE_RETENTION_DAYS` (default 7,
  conservative single-digit) bounds row age; a lifespan-owned sweeper
  (`meho_backplane.checks.evidence_retention`, the #2547 announcement-sweeper
  mould) deletes rows past the window on `CHECKS_EVIDENCE_PRUNE_INTERVAL_SECONDS`
  cadence, gated by `CHECKS_EVIDENCE_PRUNE_ENABLED`. **`0` disables the feature
  entirely** — no rows are written (`record_sensor_result` skips the append,
  gated by the runner on the same setting), restoring the pre-#2756 latest-only
  behaviour. This deliberately diverges from the announcement/topology sweepers'
  `0`=keep-forever, because an evidence history that grows forever is the
  surprise #2756 exists to prevent.
- **The trend query** (`SensorAdminService.list_results` →
  `repository.list_sensor_results`) answers the forensic question with binary
  filters only — `sensor_id` (exact), `from`/`to` (inclusive window), optional
  `state`, bounded `limit` — deterministic `evaluated_at ASC` ordering, and an
  opaque keyset `cursor`. Raw rows in a `{items, next_cursor}` envelope (#2742);
  the client aggregates. **No** server-side smoothing / downsampling / scoring
  — the `SensorResultsQuery` wire model is `extra="forbid"`, so an unknown
  filter param is a 422 (REST) / schema refusal (MCP), pinning that bound.
- **Tenant scoping**: `list_results` resolves the sensor within the caller's
  tenant first, so a cross-tenant sensor id returns `None` → 404
  `sensor_not_found` (never a 403, and never another tenant's history). The
  trend surface is own-tenant-only on all three transports (no platform-admin
  `tenant_filter`).

**Delete** is a hard `DELETE` (no tombstone) — a sensor carries no
fire-history the audit trail needs post-delete.

Surfaces: REST (`api/v1/sensors.py`, registered in `main.py`), MCP
(`mcp/tools/sensors.py`, auto-loaded), Go CLI (`cli/internal/cmd/sensor/`).
Each surface carries the four verbs — `list` / `create` / `delete` plus the
`results` trend query (#2756): `GET /api/v1/sensors/{id}/results`, the
`meho_sensor_results` MCP tool, and `meho sensor results <id>`. There is
**no** update / pause / resume path — `status` is server-initialized to
`active` at create (clients cannot supply it; a body carrying `status` is a
422) and transitions to `paused` only via #2505's runner parking.

## Dependencies

- **#2504** `meho_backplane.checks.assertions` — `AssertionSpec` (embedded
  at the wire) and `CheckState` (the five-state vocabulary). Hard
  dependency, already landed.
- `meho_backplane.scheduler.cron` — `is_valid_cron_expr`, `resolve_timezone`,
  `next_fire_after` (shared with the scheduler).
- `meho_backplane.operations._lookup` — descriptor resolution for the guard.
- Migrations `0064_create_sensor.py` (`down_revision="0063"`),
  `0070_add_sensor_confirmation_retries.py` (#2799 soft-state columns), and
  `0071_create_sensor_results.py` (`down_revision="0070"`, #2756 history table).
- **#2756** (Initiative #2780) — per-tick evidence retention + trend query;
  the retention sweeper reuses the #2547 announcement-sweeper mould.

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
- **A connector that refuses to execute must fail the dispatch, not
  return a reading.** The runner synthesizes only `unknown`, so a
  connector that answers `status="ok"` with a plausible-looking payload
  for work it never did makes the sensor state a lie. `net.*` shipped
  exactly that shape: a probe outside `MEHO_NETDIAG_PROBE_ALLOWLIST`
  returned `{"connected": false, "reason": "not_in_probe_allowlist"}`
  with `status="ok"`, so `$.connected` read `false` and the sensor
  flipped `critical` — indistinguishable from a genuine outage, with the
  `reason` dropped at the assertion select. Fixed connector-side in
  #2784 (the refusal is now the `connector_probe_refused` dispatch
  error, which lands here as `unknown` / `reason: dispatch_not_ok`), but
  the general hazard is a **connector contract** the checks layer cannot
  detect: assertion evidence is `{path, aggregate, comparator, expect,
  observed}` and carries no sibling fields from the payload, so a
  reading-shaped refusal has no in-band way to announce itself. See
  `docs/codebase/connectors-net-diagnostics.md` § *Refusal is a dispatch
  error, not a reading*.
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
