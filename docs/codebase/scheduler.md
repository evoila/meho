# Scheduler — cron + one-off agent triggers (G11.3 P2)

## Overview

The scheduler fires P1 agent runs on durable cron + one-off triggers. It is
the floor of MEHO's 24/7 operation: an operator creates a trigger row, the
scheduler loop scans for due rows on a cadence, claims them
replica-safely, and invokes the referenced agent definition through the
G11.1-T4 invocation surface.

Two of the three G11.3 trigger shapes ship in T2 (this task, issue #823):

- **Cron** — fires repeatedly on a 5-field cron expression evaluated in
  the trigger's persisted timezone.
- **One-off** — fires once at a stored instant, then transitions to a
  terminal `fired` state.

The third shape (event-subscription, T3 #824) lands as a separate
transactional-outbox table; its substrate decision is different because
events arrive asynchronously rather than on a clock boundary.

T1 (#822) is an in-flight spike that may settle the durable-execution
layer as DBOS Transact. T2 ships on the codebase's twice-stated
"roll-our-own + `pg_try_advisory_lock` + minimal deps" posture
([topology/scheduler.py](../../backend/src/meho_backplane/topology/scheduler.py),
[memory/expiry.py](../../backend/src/meho_backplane/memory/expiry.py)).
The `scheduled_trigger` row shape is substrate-neutral so a future
DBOS rebase swaps only the loop module.

## Key types

- `ScheduledTrigger` ([db/models.py](../../backend/src/meho_backplane/db/models.py))
  — the durable row. One per trigger. Columns:
  - `id`, `tenant_id` (real FK), `agent_definition_id` (real FK to
    `agent_definition` — `ondelete` is the default NO ACTION, so a
    definition cannot be removed while triggers reference it),
    `identity_sub`, `created_by_sub`.
  - `kind` (closed enum: `cron`/`one_off`), `cron_expr` (NULL for
    one-off), `fire_at` (one-off scheduled instant, NULL for cron),
    `timezone` (IANA name, default `UTC`).
  - `next_fire_at` (the hot column the loop scans, populated for
    both cron and one-off), `last_fired_at`, `status` (closed enum:
    `active`/`paused`/`cancelled`/`fired`).
  - `inputs` (JSON-shaped, nullable, passed to the agent as the run's
    input string).
- `ScheduledTriggerKind`, `ScheduledTriggerStatus` — closed StrEnums
  kept in lock-step with the DB-layer `CHECK (... IN (...))` constraints
  via migrations `0020` (T1 substrate) and `0021` (T2 dispatcher
  columns + widened `status` CHECK).
- Scheduler package
  ([scheduler/](../../backend/src/meho_backplane/scheduler)):
  - `scheduler.cron` — `next_fire_after()` (croniter wrapper),
    `is_valid_cron_expr()`, `InvalidCronExpressionError`,
    `InvalidTimezoneError`.
  - `scheduler.repository` — `create_cron_trigger()`,
    `create_one_off_trigger()`, `claim_due_triggers()` (PG `SELECT ...
    FOR UPDATE SKIP LOCKED`), `advance_cron_trigger()`,
    `mark_one_off_fired()`.
  - `scheduler.loop` — `run_one_tick()`, `start_scheduler()`,
    `stop_scheduler()`. The forever loop the FastAPI lifespan owns.

## Control flow

```
                                    +-----------------------------+
   FastAPI lifespan startup    --->  |  start_scheduler()          |
                                    |  asyncio.create_task(...)   |
                                    +-----------------------------+
                                                 |
                                                 v
                              +------------------------------------------+
                              | _scheduler_loop()                        |
                              | while True:                              |
                              |   await sleep(tick_interval_seconds)     |
                              |   await run_one_tick()                   |
                              +------------------------------------------+
                                                 |
                                                 v
                  +------------------------------------------+
                  | run_one_tick()                           |
                  | 1. pg_try_advisory_lock(MEHOSCHD)        |
                  |    (no-op on SQLite test path)           |
                  | 2. claim_due_triggers(now=now(UTC),      |
                  |       limit=50)                          |
                  |    -- SELECT ... FOR UPDATE SKIP LOCKED  |
                  | 3. for each row:                         |
                  |      kind==cron: advance + invoke        |
                  |      kind==one_off: mark fired + invoke  |
                  | 4. pg_advisory_unlock                    |
                  +------------------------------------------+
                                                 |
                                                 v
                              +------------------------------------------+
                              | AgentInvoker.run(operator, name, inputs, |
                              |     async_mode=True,                     |
                              |     trigger=AgentRunTrigger.SCHEDULED)   |
                              +------------------------------------------+
```

### Cron fire path (`_fire_cron`)

1. `advance_cron_trigger(row, fire_instant=now)`:
   - Compute the next cron match via `croniter` from `now` in the
     trigger's timezone.
   - Conditional `UPDATE` (`WHERE id=:id AND status='active' AND
     next_fire_at=:previous_next`) sets `next_fire_at=new_next` and
     `last_fired_at=now`.
   - Returns `None` when zero rows match (another claimer beat this
     replica to it) — skip the fire.
2. Commit the advance.
3. `_invoke_agent()` resolves the agent definition (FK lookup —
   `agent_definition_id` is a real FK, so this `SELECT` is by primary
   key under the definition-NOT-NULL invariant), kicks off the agent
   run in async mode, returns.

### One-off fire path (`_fire_one_off`)

1. `mark_one_off_fired(row, fire_instant=now)`:
   - Conditional `UPDATE` (`WHERE id=:id AND status='active' AND
     next_fire_at=:previous_next`) sets `status='fired'`,
     `last_fired_at=now`.
   - Returns `None` when another claimer beat this replica to it.
2. Commit, then invoke.

### Replica-safety property

Two backplane pods sharing one Postgres are coordinated by two layers:

1. **Process-wide advisory lock** (`pg_try_advisory_lock(MEHOSCHD)`):
   only one replica's loop runs the tick body at a time. The losing
   replica's `try_advisory_lock` returns `False` and the tick is skipped.
   Non-blocking.
2. **Per-row claim under `FOR UPDATE SKIP LOCKED`** plus the conditional
   `UPDATE` in the advance/mark-fired step. Belt-and-braces — even if the
   advisory lock were removed, single-fire would still hold across
   concurrent claimers because the conditional `UPDATE` matches zero rows
   on the loser side.

On SQLite (the unit-test path) both layers no-op. The
two-concurrent-ticks test
([tests/test_scheduler.py](../../backend/tests/test_scheduler.py))
launches two `run_one_tick` coroutines on the same DB; the
conditional-`UPDATE` discipline is what enforces single-fire and
exercises the same guard that runs on PG when advisory-lock acquisition
ordering creates a brief window of double-claim possibility.

### Restart durability

State lives in the `scheduled_trigger` row. On pod restart:

- A cron trigger whose `next_fire_at` has already passed fires once on
  the next tick, advances to the next cron match, resumes normal cadence.
  No catch-up storm — a 24-hour outage on `*/5 * * * *` fires exactly
  once on resume rather than 288 times.
- A one-off trigger whose `next_fire_at` has already passed fires once
  on the next tick and transitions to `fired`.

The "compute next then fire" discipline (advance BEFORE invoking the
agent) is what guarantees this: even a slow agent run cannot delay the
next tick, because `next_fire_at` is already persisted at the next
scheduled instant by the time the agent run starts.

## Dependencies

- **`croniter` 6.x** — pure-Python, ~1.5 kLoC, single-purpose, MIT
  licensed. The only new runtime dependency this task introduces.
  Confined to `scheduler.cron`; the rest of the codebase depends on the
  module-level `next_fire_after()` / `is_valid_cron_expr()` seam.
- **`AgentInvoker.run(..., trigger=AgentRunTrigger.SCHEDULED)`** (G11.1-T4)
  — the invocation surface grew a `trigger` keyword alongside this task
  so the durable `agent_run` row's provenance column reflects the
  scheduler vs. a direct REST/MCP/CLI invocation.
- **`pg_try_advisory_lock` / `pg_advisory_unlock`** — Postgres
  session-level advisory locks. No-op on SQLite via dialect gate.
- **`SELECT ... FOR UPDATE SKIP LOCKED`** — SQLAlchemy 2.0
  `with_for_update(skip_locked=True)`; emitted only on PG.

## Settings

| Setting (env var) | Default | Notes |
|---|---|---|
| `SCHEDULER_ENABLED` | `true` | Lifespan skips starting the loop when `false`. Operators with an external orchestrator can opt out. |
| `SCHEDULER_TICK_INTERVAL_SECONDS` | `30` | Cadence of the scan-for-due loop. Floor 1 s, ceiling 3600 s. 30 s is the consumer-doc-accepted granularity (cron's finest field is a minute). |

## Known issues / limitations

- **One-off resolution is "to the second"** — `next_fire_at <= now`
  semantics fire as soon as the tick after the scheduled instant runs.
  With a 30 s tick, a one-off scheduled for `12:00:00` fires somewhere in
  `[12:00:00, 12:00:30]`. Cron has the same semantics: `0 12 * * *`
  fires in `[12:00:00, 12:00:30]`. Tightening this needs a smaller tick;
  the loop is bounded by `_CLAIM_BATCH_LIMIT=50` rows per tick to keep
  per-tick wall-clock cost low.
- **Catch-up policy is "one fire on resume"** — a long outage does not
  replay every missed cron instant. The consumer doc accepts this; an
  operator who needs "fire-every-N-runs" semantics writes that into the
  agent definition itself.
- **No admin surface yet** — create / list / cancel / pause flows ship
  in G11.3-T5 (#826). T2 exposes the repository functions so tests can
  construct triggers; production deploys insert via SQL or a future
  T5 API.
- **`AgentRunTrigger.SCHEDULED` provenance** — passed through to
  `AgentInvoker.run`'s new `trigger` kwarg. Audit queries that filter by
  trigger see scheduled runs distinctly from direct invocations.

## References

- Issue #823 (G11.3-T2 cron + one-off triggers)
- Initiative #804 (G11.3 Scheduler)
- Goal #800 (G11 Agentic ops runtime)
- Sibling tasks: #822 (T1 substrate-decision spike), #824 (T3 event
  trigger), #825 (T4 in-flight resume), #826 (T5 admin surface)
- Precedent loops:
  [topology/scheduler.py](../../backend/src/meho_backplane/topology/scheduler.py),
  [memory/expiry.py](../../backend/src/meho_backplane/memory/expiry.py)
- Migrations:
  - `alembic/versions/0020_create_scheduled_trigger.py` — T1 #1064
    storage substrate (table, indexes, kind / status / in_flight_policy
    CHECKs, discriminated-union kind-fields CHECK).
  - `alembic/versions/0021_scheduled_trigger_dispatcher_columns.py` —
    T2 this PR (`identity_sub`, `inputs`, `timezone` columns;
    `status` CHECK widened to admit `fired`).
- Tests:
  [tests/test_scheduler.py](../../backend/tests/test_scheduler.py),
  [tests/test_migration_0020_scheduled_trigger.py](../../backend/tests/test_migration_0020_scheduled_trigger.py),
  [tests/test_migration_0021_scheduled_trigger.py](../../backend/tests/test_migration_0021_scheduled_trigger.py),
  [tests/test_db_scheduled_trigger.py](../../backend/tests/test_db_scheduled_trigger.py)
