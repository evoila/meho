# agent-run — durable agent-invocation record + enforced lifecycle

## Overview

`agent_run` is the durable record of one LLM-agent invocation hosted in
MEHO's process (Initiative #802, G11.1 Agent runtime; Task #813, T6).
The runtime executes an agent's tool-use loop in-process (G11.1-T1);
each invocation is one `agent_run` row that ties the session's tool
calls together, makes a run inspectable + cancellable, and seeds the
audit/replay lineage.

The row's `id` **is** the `agent_session_id` lineage key that G11.4/C2
binds into every per-tool-call audit row (mirroring the
`audit_log.agent_session_id` column added by migration `0014`). A caller
that creates a run threads `run.id` through the runtime so every audit
row written during that run shares it; `meho audit replay <session-id>`
then reconstructs the timeline.

Two pieces ship in T6:

- The **ORM model** `AgentRun` + migration `0015` — storage only, no
  helper logic on the class (the discipline `AuditLog` / `WebSession`
  follow).
- The **lifecycle service**
  `backend/src/meho_backplane/operations/agent_run.py` — create /
  inspect / transition / cancel, with an explicit, enforced state
  machine.

What is **not** in T6 (other G11.1 tasks own these):

- The invocation surface (sync + async handle/poll/SSE on REST + MCP +
  CLI) — G11.1-T4 (#811). T4 calls this service.
- The Pydantic-AI loop seam that actually runs the agent — G11.1-T1
  (#808).
- Per-tool-call raw+redacted audit rows + replay — G11.4/C2 (this is the
  run-level record those rows link to via `agent_session_id`).
- Cost computation — G11.5/C3. The `cost` column is recorded here but
  stays NULL in v0.2; `succeed_run` already accepts a `cost` argument so
  C3 lands without a service-signature change.

## Key types

`meho_backplane.db.models`:

- `AgentRun` — the table. Columns: `id` (PK, doubles as the session
  lineage key), `agent_definition_id` (soft-FK to the G11.1-T2 table
  landing in parallel #809), `tenant_id` (real FK to `tenant.id`),
  `identity_sub` / `identity_act` (the RFC 8693 delegation pair —
  `sub` = principal acted for, `act` = agent acting, nullable for a
  direct human run), `trigger`, `model_tier` (logical tier; the G11.5
  resolver maps it to a concrete pair), `provider` / `model` (resolved,
  nullable until `start`), `status`, `turns`, `cost` (stub until C3),
  `output`, `error`, `parent_run_id` (self-referential soft-FK for
  agent-invoked child runs, G11.1-T5), `created_at`, `started_at`,
  `ended_at`.
- `AgentRunStatus` — closed `StrEnum`: `pending`, `running`,
  `awaiting_approval`, `succeeded`, `failed`, `cancelled`.
- `AgentRunTrigger` — closed `StrEnum`: `direct`, `scheduled`, `event`,
  `agent-invoked`.

Both enums are backed by DB-layer `CHECK (col IN (...))` constraints
that move in lock-step with the enum (`ck_agent_run_status`,
`ck_agent_run_trigger`); the drift guards in `tests/test_db_agent_run.py`
assert the model enum, the model-side `_AGENT_RUN_STATUSES` /
`_AGENT_RUN_TRIGGERS` tuples, and the migration's frozen literal tuples
all agree. This is the same closed-enum + CHECK discipline `GraphEdgeKind`
/ `GraphHistoryChangeKind` follow.

`meho_backplane.operations.agent_run` (the lifecycle service):

- `ALLOWED_TRANSITIONS: dict[AgentRunStatus, frozenset[AgentRunStatus]]`
  — the single source of truth for legal `status` edges.
- `TERMINAL_STATUSES: frozenset[AgentRunStatus]` — `succeeded`,
  `failed`, `cancelled`.
- Exceptions: `AgentRunError` (base), `AgentRunNotFoundError` (404-class),
  `IllegalTransitionError` (409-class — carries the rejected
  `from`/`to` pair), `UnauthorizedCancellationError` (403-class).

## Control flow

The state machine:

```
pending ──> running ──> succeeded   (terminal)
   │           │  ▲         │
   │           │  │         └─ output recorded
   │           ▼  │
   │     awaiting_approval          (resumable: ──> running)
   │           │  │
   │           ▼  ▼
   ├────────> failed                (terminal)
   │
   └──┐
      ▼
   cancelled                        (terminal; from any
                                     non-terminal state, by an
                                     authorized operator)
```

Why an explicit state machine and not just the `CHECK`: a `CHECK`
enforces the legal *set* of status values, not the legal *transitions*.
Without a guard, a runtime bug could write `succeeded` -> `running` (a
finished run restarting) or `cancelled` -> `succeeded` (a cancelled run
reporting success), corrupting the lineage and any cost / replay view.
`transition()` consults `ALLOWED_TRANSITIONS` and raises
`IllegalTransitionError` **before** any DB write, so an illegal edge
never lands.

Service entry points (all async; all take an open `AsyncSession`, flush,
and leave the commit to the caller — the same transaction contract
`session_store` uses, so a transition can compose with other writes in
one transaction):

- `create_run(session, *, tenant_id, identity_sub, trigger, model_tier,
  identity_act=None, agent_definition_id=None, parent_run_id=None)
  -> AgentRun` — inserts a `pending` row; `run.id` is the lineage key.
- `get_run(session, run_id) -> AgentRun | None` — read side; returns
  `None` for a missing id (the caller shapes the 404).
- `transition(session, row, to_status) -> AgentRun` — the single
  status-mutation point. Stamps `started_at` on the first entry into
  `running` (the `awaiting_approval` -> `running` resume does not reset
  it) and `ended_at` on any terminal state.
- `start_run(session, row, *, provider, model)` — records the resolved
  provider+model, then `pending`/`awaiting_approval` -> `running`.
- `increment_turns(session, row)` — bumps the observable turn counter;
  no status change. The turn *budget* is enforced by the loop
  (`UsageLimits.request_limit`, G11.1-T1), not this column.
- `succeed_run(session, row, *, output, cost=None)` — records output (+
  optional C3 cost), -> `succeeded`.
- `fail_run(session, row, *, error)` — records the failure reason
  (kept distinct from `output` so diagnostics never masquerade as a
  result), -> `failed`.
- `cancel_run(session, run_id, *, operator) -> AgentRun` — the
  operator-authorized cancellation path. Requires at least
  `TenantRole.OPERATOR` (cancelling in-flight work is a control action,
  not a read); a `read_only` operator gets `UnauthorizedCancellationError`.
  Cancelling an already-terminal run surfaces as `IllegalTransitionError`
  (not a silent no-op). The function records the **durable intent**
  (`status='cancelled'` + `ended_at`); the actual interruption of the
  in-flight async loop is the runtime's job (G11.1-T1) — the loop
  observes the cancelled status at its next turn boundary and stops.
  Recording the intent durably first is what makes cancellation survive
  a process restart.

## Dependencies

- `meho_backplane.db.models` — the ORM model + enums.
- `meho_backplane.db.engine` — `get_sessionmaker` for the per-call
  session (callers usually pass their own).
- `meho_backplane.auth.operator` — `Operator` + `TenantRole` for the
  cancel authorization check. The service uses its own private linear
  role-ranking tuple (`_ROLE_RANK`) rather than importing
  `auth.rbac._ROLE_ORDER` (a private HTTP-RBAC constant) so the service
  layer does not depend on the HTTP module's internals.

## Known issues / notes

- The `agent_definition_id` and `parent_run_id` columns are **soft-FKs**
  (no FK clause). `agent_definition` (G11.1-T2 / #809) lands in a sibling
  task in parallel; a hard FK would couple the two migrations' ordering.
  Tightening to a real FK is deferred to a future migration once both
  tables are settled — the same soft-FK discipline `audit_log.target_id`
  / `audit_log.parent_audit_id` follow.
- `cost` is a stub (`Numeric(12, 6)`, NULL in v0.2) until G11.5/C3.
- SQLite (the dev/test driver) returns naive datetimes from
  `timestamptz` columns; the chassis-wide "naive means UTC" convention
  applies. PG returns tz-aware values (covered by the testcontainers
  replay suite).

## References

- Initiative #802 (G11.1 Agent runtime), Task #813 (T6). Parent Goal
  #800 (G11 agentic ops runtime).
- Migration `0015_create_agent_run.py`; model `AgentRun` in
  `backend/src/meho_backplane/db/models.py`; service
  `backend/src/meho_backplane/operations/agent_run.py`.
- Lineage-key sibling: `audit_log.agent_session_id` (migration `0014`,
  G8.2-T1 #1009) — the column `agent_run.id` is consumed as.
- Tests: `tests/test_db_agent_run.py`,
  `tests/test_migration_0015_agent_run.py`,
  `tests/test_agent_run_lifecycle.py`.
