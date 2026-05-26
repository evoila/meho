# agent-run — durable agent-invocation record + enforced lifecycle + in-flight reaper

## Overview

`agent_run` is the durable record of one LLM-agent invocation hosted in
MEHO's process (Initiative #802, G11.1 Agent runtime; Task #813, T6).
The runtime executes an agent's tool-use loop in-process (G11.1-T1);
each invocation is one `agent_run` row that ties the session's tool
calls together, makes a run inspectable + cancellable, and seeds the
audit/replay lineage. Initiative #804 (G11.3 Scheduler), Task #825 (T4)
adds the **lease/heartbeat + reaper** that guarantees a run killed
mid-flight (pod restart, OOM, network partition) ends in a terminal
audited state — never silently lost.

The row's `id` **is** the `agent_session_id` lineage key that G11.4/C2
binds into every per-tool-call audit row (mirroring the
`audit_log.agent_session_id` column added by migration `0014`). A caller
that creates a run threads `run.id` through the runtime so every audit
row written during that run shares it; `meho audit replay <session-id>`
then reconstructs the timeline.

Two pieces shipped in T6:

- The **ORM model** `AgentRun` + migration `0017` — storage only, no
  helper logic on the class (the discipline `AuditLog` / `WebSession`
  follow).
- The **lifecycle service**
  `backend/src/meho_backplane/operations/agent_run.py` — create /
  inspect / transition / cancel, with an explicit, enforced state
  machine.

T4 (#825) extends both with the in-flight-reclaim contract:

- **Three columns** added to `AgentRun` via migration `0026_add_agent_run_lease_reaper`:
  `lease_owner` (nullable Text — the worker holding the run),
  `lease_expires_at` (nullable `timestamptz` — the heartbeat deadline),
  `in_flight_policy` (NOT NULL Text with a `CHECK` constraint backed
  by `ScheduledTriggerInFlightPolicy` — the per-run snapshot of the
  firing trigger's `resume | fail_into_audit` policy).
- **Lease lifecycle helpers** in the service:
  `claim_lease` / `heartbeat` / `release_lease` / `snapshot_in_flight_policy`,
  plus a `LeaseLostError` the worker uses to abort on a stolen lease.
  `transition()` clears the lease as a terminal-state side effect so
  the partial index does not retain zombie entries.
- **The reaper** at `backend/src/meho_backplane/agent/reaper.py` — an
  `asyncio` lifespan-owned background task that scans for
  `status='running' AND lease_expires_at < now()` on a fixed cadence
  and applies the per-run policy: `fail_into_audit` transitions the
  row to `failed` with a stable interruption reason; `resume` clears
  the lease so the next dispatcher sweep re-claims and a fresh
  worker continues. Both outcomes write an audit row in the same
  transaction as the lifecycle change, so a crash between the two
  cannot leave a reaped run without an audit row.

What is **not** in T6 or T4 (other tasks own these):

- The invocation surface (sync + async handle/poll/SSE on REST + MCP +
  CLI) — G11.1-T4 (#811). T4 calls this service.
- The Pydantic-AI loop seam that actually runs the agent — G11.1-T1
  (#808). The agent loop must call `heartbeat()` at ≈ `ttl_seconds/2`
  cadence so the reaper does not reclaim a healthy run; the loop must
  also honour `LeaseLostError` by aborting immediately (any further
  side effects would be at-least-twice).
- The trigger-firing path that calls `claim_lease` +
  `snapshot_in_flight_policy` at run-start — G11.3-T2 (#823, cron + one-off)
  and G11.3-T3 (#824, event subscription). T4 ships the substrate;
  T2/T3 wire the call site.
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
- `lease_owner` / `lease_expires_at` (T4 #825) — the worker-identifier +
  heartbeat-deadline pair the scheduler writes when a worker begins
  executing a run. Both NULL when no worker holds it (`pending`,
  `awaiting_approval` after release, or any terminal state). The
  lifecycle service keeps the two columns in lock-step.
- `in_flight_policy` (T4 #825) — NOT NULL Text, server-default
  `'fail_into_audit'`, `CHECK` constraint binding to
  `ScheduledTriggerInFlightPolicy`. Per-run snapshot of the firing
  trigger's policy copied at run-start so a definition edit mid-flight
  cannot flip behaviour on a run already executing.

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
- `claim_lease(session, row, *, owner, ttl_seconds)` — stamp
  `lease_owner` + `lease_expires_at` on a run a worker is about to
  execute (T4 #825). Status untouched — composed by the caller with
  `start_run` inside one transaction so a partial commit cannot leave
  the row `running` without a lease.
- `heartbeat(session, *, run_id, owner, ttl_seconds) -> AgentRun` —
  extend the lease iff the conditional `UPDATE … WHERE lease_owner =
  owner AND status = 'running'` touches one row; zero rows raises
  `LeaseLostError` so a worker whose lease was stolen by the reaper
  (or whose run was cancelled out from under it) stops cleanly. The
  conditional UPDATE is atomic at the DB layer — a Python-side check
  would race the reaper.
- `release_lease(session, row)` — clear both lease columns without
  changing status. Used by the reaper's `resume` policy and by the
  terminal-state side effect in `transition()`.
- `snapshot_in_flight_policy(session, row, policy)` — copy the firing
  trigger's `ScheduledTriggerInFlightPolicy` onto the run row at
  run-start. T2/T3 call this inside the same transaction as the
  initial `create_run` so the run never executes without a snapshotted
  policy.

## In-flight reaper (T4 #825)

`backend/src/meho_backplane/agent/reaper.py` owns the *reclaim* half of
the no-silently-lost contract. The trigger-firing path (T2 / T3) writes
the lease at run-start; the healthy worker bumps it forward via
`heartbeat`; the reaper scans for expired leases on a fixed cadence
(`AGENT_RUN_REAPER_TICK_INTERVAL_SECONDS`, default 30s) and applies the
per-run `in_flight_policy`:

- **`fail_into_audit`** — the conservative default. Transition the run
  to `failed` with the stable `AGENT_RUN_REAPER_INTERRUPTION_REASON`
  string, plus an audit row at path
  `internal/agent-run/reaper/fail-into-audit`. The next trigger tick
  fires a fresh run.
- **`resume`** — `release_lease` so the next dispatcher sweep
  re-claims; status stays `running` (the row's current state IS the
  resume point), plus an audit row at path
  `internal/agent-run/reaper/clear-for-resume`. At-least-once
  semantics; the agent's tool calls should be idempotent-friendly
  (G11.1 design constraint).

Shape mirrors the chassis's other lifespan-owned sweepers
(`memory/expiry.py`, `topology/scheduler.py`):

- `asyncio.create_task` started in `_BackgroundTasks` and cancelled on
  lifespan shutdown. Strong-reference retention so the task isn't
  garbage-collected mid-flight ("Task was destroyed but it is
  pending!").
- Sleep-then-sweep so the first tick does not race startup work
  (eager engine init, embedding preload, typed-op registration).
- Per-tick try/except + per-row try/except so one bad row never
  stalls the batch and one transient DB blip never crashes the loop
  (a crashed loop silently stops the reclaim contract — which is
  exactly what we're protecting against).
- Postgres advisory lock (`pg_try_advisory_lock`) for single-flighting
  across replicas; SQLite (dev/test) treats it as no-op.
- LIMIT bound (`AGENT_RUN_REAPER_MAX_PER_TICK`, default 50) so a
  post-outage backlog drains across multiple ticks rather than
  pinning one Postgres backend.

The reaper writes its audit row via a private staging helper
(`_stage_audit_row`) rather than the chassis's shared writers because
the audit row + the lifecycle transition must commit in the **same
transaction** — the chassis's shared writers each open their own
session, which would split the reap across two commits and could
leave a reaped run without an audit row if the second commit failed.

Settings (all in `Settings`, all opt-out via env):

- `AGENT_RUN_REAPER_ENABLED` (default `true`) — disable the in-tree
  reaper when running an external lease-reclaim mechanism (DBOS
  Transact, a workflow engine).
- `AGENT_RUN_REAPER_TICK_INTERVAL_SECONDS` (default 30, range 5-3600).
- `AGENT_RUN_REAPER_MAX_PER_TICK` (default 50, range 1-1000).
- `AGENT_RUN_LEASE_TTL_SECONDS` (default 60, range 10-3600) — the
  initial lease window callers pass to `claim_lease`. With the
  default 30s tick + 60s TTL, a worker has two heartbeat windows of
  slack before reclaim — a transient 20s GC pause or network blip
  does not cost a run.

## Dependencies

- `meho_backplane.db.models` — the ORM model + enums.
- `meho_backplane.db.engine` — `get_sessionmaker` for the per-call
  session (callers usually pass their own).
- `meho_backplane.auth.operator` — `Operator` + `TenantRole` for the
  cancel authorization check. The service uses its own private linear
  role-ranking tuple (`_ROLE_RANK`) rather than importing
  `auth.rbac._ROLE_ORDER` (a private HTTP-RBAC constant) so the service
  layer does not depend on the HTTP module's internals.
- (Reaper only) `meho_backplane.db.models.AuditLog` — staged directly
  in the same transaction as the lifecycle transition.

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
- The reaper's `resume` policy is **at-least-once** by construction:
  the original worker may still be alive (network-partitioned,
  long GC pause) when the reaper clears its lease. The first defence
  is `heartbeat`'s conditional `UPDATE` (raises `LeaseLostError` so a
  partitioned worker stops on its next heartbeat). The second defence
  is the agent runtime's idempotency-friendly tool calls (G11.1's
  design constraint) — T4 does not enforce idempotency at the
  substrate, it is the agent author's contract.
- Migration `0026_add_agent_run_lease_reaper` collides numerically
  with PR #1065 (G11.3-T2, cron + one-off triggers) which also numbers
  its migration `0026`. Both PRs are in-flight in parallel; whichever
  lands second rebases its migration to `0026`.

## References

- Initiative #802 (G11.1 Agent runtime), Task #813 (T6). Parent Goal
  #800 (G11 agentic ops runtime).
- Initiative #804 (G11.3 Scheduler), Task #825 (T4 — lease/heartbeat
  + reaper).
- Migration `0017_create_agent_run.py` (T6 — table) +
  `0026_add_agent_run_lease_reaper.py` (T4 — lease columns); model
  `AgentRun` in `backend/src/meho_backplane/db/models.py`; service
  `backend/src/meho_backplane/operations/agent_run.py`; reaper
  `backend/src/meho_backplane/agent/reaper.py`.
- Lineage-key sibling: `audit_log.agent_session_id` (migration `0014`,
  G8.2-T1 #1009) — the column `agent_run.id` is consumed as.
- Failure-isolation precedent: `memory/expiry.py` (per-tick try/except
  + lifespan-owned sweeper) and `topology/scheduler.py` (advisory-lock
  single-flighting).
- Consumer doc: `agent-runtime-for-ops-spec.md` §P2 (the explicitly
  accepted `fail_into_audit` default).
- Tests: `tests/test_db_agent_run.py`,
  `tests/test_migration_0017_agent_run.py`,
  `tests/test_agent_run_lifecycle.py`,
  `tests/test_migration_0026_agent_run_lease_reaper.py`,
  `tests/test_agent_run_reaper.py`.
