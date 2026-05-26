# Event-subscription trigger — transactional outbox + drain (G11.3-T3 #824)

The third scheduler trigger shape: an agent run fires in response to a
MEHO-internal event (an audit predicate match, a connector alert, or
another agent run reaching a terminal state). The scheduler's cron +
one-off paths fire on a *clock* boundary ([scheduler.md](scheduler.md));
this path fires on an *event* boundary, which needs a different
substrate.

## Why a transactional outbox, not raw `LISTEN`/`NOTIFY`

The research note on #824 settled this before any code landed:
PostgreSQL `LISTEN`/`NOTIFY` is **not durable**. A `NOTIFY` fired while
no listener is connected is silently lost — the PG docs are explicit
([sql-notify.html](https://www.postgresql.org/docs/current/sql-notify.html)).
A multi-replica deployment that rolls a pod or evicts during a deploy
has a window with zero listeners; relying on `NOTIFY` for delivery
guarantees would silently drop escalations every release.

The PG-docs-recommended pattern is the textbook transactional outbox:
the producer writes an `event_outbox` row in the **same DB
transaction** as the event-producing state change. A separate drain
loop scans the outbox via `FOR UPDATE SKIP LOCKED`, claims unprocessed
rows, dispatches them, marks them processed. A pod restart loses
nothing: the rows are on disk and the next poll picks them up.
Multi-replica safety follows from `SKIP LOCKED` — two concurrent
drains never see the same row.

`LISTEN`/`NOTIFY` is layered on top as a **latency hint only**: the
producer's commit triggers a `NOTIFY` that wakes the drain's sleep
early, dropping per-event latency from the next 5-10s tick to
sub-second. A dropped notification is benign — the next polled tick
picks the row up anyway. The correctness primitive is the row on
disk; `NOTIFY` is a tail-latency optimisation.

## What's in the box

```
backend/src/meho_backplane/events/
├── __init__.py        # re-exports publish, start_event_drain, stop_event_drain
├── outbox.py          # producer-side publish() + post-commit NOTIFY hint
└── drain.py           # background drain loop + advisory-lock + claim + dispatch
```

Plus the persistence shape in
[backend/src/meho_backplane/db/models.py](backend/src/meho_backplane/db/models.py):

- `EventOutbox` — one row per emitted event. Columns: `event_id` (PG
  `BIGSERIAL` / SQLite `Integer`), `tenant_id` (FK), `event_kind`
  (free-text discriminator), `payload` (JSONB / JSON), `created_at`,
  `claimed_at` (observability), `claimed_by` (observability),
  `processed_at` (`NULL` until claimed and dispatched), and a partial
  index `(processed_at, event_id)` on `processed_at IS NULL` to drive
  the drain's claim query.
- `EVENT_OUTBOX_NOTIFY_CHANNEL` — the PG channel name the producer
  side `NOTIFY`s on and the drain side `LISTEN`s on.

Migration: `backend/alembic/versions/0026_create_event_outbox.py`
(revises `0025`, which is #1065's scheduler dispatcher-columns
extension).

## The producer side — `publish()`

The single function `events.publish` is the producer-side surface.
Call it inside the producer's *open* `AsyncSession` so the outbox
INSERT shares the producer's commit:

```python
from meho_backplane.events import publish as publish_event

async def succeed_run(session, run, output):
    # ... mutate the agent_run row ...
    await publish_event(
        session,
        tenant_id=run.tenant_id,
        event_kind="agent_run.completed",
        payload={
            "run_id": str(run.id),
            "agent_definition_id": str(run.agent_definition_id),
            "status": "succeeded",
            "tenant_id": str(run.tenant_id),
        },
    )
    # caller commits; both the agent_run UPDATE and the outbox
    # INSERT land in the same transaction
```

Same-transaction discipline is the **load-bearing invariant** of the
whole feature:

- If the producer's transaction commits, both the state change and
  the outbox row are durable; the drain picks the row up on the next
  tick (or sooner via the `NOTIFY` hint).
- If the producer's transaction rolls back, the outbox row rolls back
  with it; no orphan event is ever dispatched for state that didn't
  land.

`publish` flushes (so `event_id` is populated) but does not commit —
the caller's transaction owns the commit. Two tests pin this
contract:

- `test_publish_rolls_back_with_producer` — explicit rollback on the
  shared session discards both the producer's INSERT and the outbox
  row.
- `test_terminal_event_rolls_back_with_run_transition` — exercises
  the same invariant at the real call site (`operations/agent_run.py`
  terminal transition).

### Post-commit `NOTIFY` hint

`publish` attaches a one-shot `after_commit` listener via SQLAlchemy
events. When the producer's transaction commits, the listener opens a
*fresh* short-lived connection through the engine and fires
`NOTIFY <channel>`. Notes:

- **Why a fresh connection, not the producer's:** the producer's
  connection may already be returned to the pool by the time
  `after_commit` fires (FastAPI's request-scoped session dependency
  shape). `NOTIFY` is fire-and-forget so the cost of `engine.connect()`
  + immediate close is acceptable.
- **`once=True` de-duplication:** a batched producer that publishes
  N events in one transaction notifies *once*, not N times. Cheap
  and avoids notify-storms.
- **Dialect-gated:** the listener body checks `dialect.name ==
  "postgresql"` and silently skips on SQLite (the unit-test path) —
  same gate the scheduler's advisory-lock path uses.
- **Failure is silent:** if the `NOTIFY` `engine.connect()` raises,
  the listener swallows it. The durable channel is the outbox row;
  `NOTIFY` is a latency hint and never blocks producer commit
  success.

## The drain side — the loop

`drain.py`'s background `asyncio` task is wired into the FastAPI
lifespan via `start_event_drain` / `stop_event_drain` in
[backend/src/meho_backplane/main.py](backend/src/meho_backplane/main.py).
It is gated on `EVENT_DRAIN_ENABLED=true` (default), the same shape as
the scheduler / topology-refresh / memory-expiry / grant-expiry
sweepers.

On each tick (default 10s, settable via
`EVENT_DRAIN_TICK_INTERVAL_SECONDS`):

1. **Claim the process-wide advisory lock.**
   `pg_try_advisory_lock(0x4D45_484F_4556_5442)` — see
   [Advisory-lock keys](#advisory-lock-keys) below. Only one
   replica's drain runs the tick body at a time. A second replica's
   tick is a no-op until the holder releases.

2. **Scan + claim unprocessed rows** via:

   ```sql
   SELECT * FROM event_outbox
   WHERE processed_at IS NULL
   ORDER BY event_id
   LIMIT :batch
   FOR UPDATE SKIP LOCKED
   ```

   `SKIP LOCKED` is the belt-and-braces guarantee: even with the
   advisory-lock guard removed, two concurrent claimers never receive
   the same row. The partial index
   `event_outbox_unprocessed_idx ON (processed_at, event_id) WHERE
   processed_at IS NULL` keeps the claim query O(log unprocessed)
   rather than O(total rows).

3. **Stamp `claimed_at` + `claimed_by`** (observability — the
   `claimed_by` column carries `pod_name` so a stuck poll can be
   traced to the holding replica). The claim is a separate UPDATE on
   the same open session as the SELECT; the FOR UPDATE row-lock
   carries through to the UPDATE.

4. **Dispatch each row.** In v0.2 the subscription-matcher is not
   yet built (see [Deferred work](#deferred-work) below). The drain
   therefore stamps `processed_at` directly and emits a structlog
   event — events are visible in the log stream, and the drain's
   stamp-and-skip behaviour proves the substrate works end-to-end.

5. **Mark each row processed** via a conditional UPDATE
   (`processed_at IS NULL` predicate). The conditional UPDATE is the
   single-processing invariant on SQLite where SKIP LOCKED is a
   no-op; on PG it's defensive belt-and-braces. Per-row failure is
   isolated by an inner `try`/`except` so one bad row never stalls
   the rest of the tick.

6. **`LISTEN` for `NOTIFY`** (concurrent with the polling sleep).
   The drain holds a long-lived engine connection on which it
   `LISTEN <channel>`s; a producer's post-commit `NOTIFY` lands as a
   wake-up that cuts the sleep short. The next iteration starts
   immediately rather than waiting for the next tick boundary. The
   listener is **not durable** — a NOTIFY missed during a drain
   restart is benign because the polling cadence picks the row up
   anyway. Reconnect-with-backoff for the listen connection is a
   v0.3 polish item; the current shape degrades to polling-only on
   listen-connection failure with no retry.

## Advisory-lock keys

Two PG advisory locks coexist on the same DB; each has a distinct
63-bit signed-int key so the locks never collide:

| Key | Hex | ASCII | Owner |
|---|---|---|---|
| `_SCHEDULER_ADVISORY_LOCK_KEY` | `0x4D45_484F_5343_4844` | `MEHOSCHD` | `scheduler/loop.py` (cron + one-off, T2 #1065) |
| `_EVENT_DRAIN_ADVISORY_LOCK_KEY` | `0x4D45_484F_4556_5442` | `MEHOEVTB` | `events/drain.py` (event outbox, this Task) |

Distinct keys mean both loops can run concurrently without starving
each other — the scheduler's lock holder does not block the drain's
claim, and vice versa. The ASCII spellings make the keys easy to
recognise in `pg_locks` during operator triage.

## Deferred work

### Subscription matcher (depends on T5 #826)

The drain loop in v0.2 stamps `processed_at` directly without
matching subscribers. The matcher — looking up `scheduled_trigger`
rows where `kind='event'` and `event_filter` matches `payload` — is
deferred because:

- The trigger-creation surface that populates `kind='event'` rows is
  the admin surface in T5 #826. Until T5 ships, no `event` trigger
  exists, so the matcher would always return zero matches.
- The substrate proof — durable across restart, no double-fire,
  same-transaction discipline — is independent of the matcher and is
  the load-bearing correctness property this Task ships.

When T5 lands, the matcher folds into the dispatch step: for each
claimed row, `SELECT ... FROM scheduled_trigger WHERE kind='event' AND
event_filter @> payload`, then fire each matched trigger through
`AgentInvoker.run_scheduled` (the same seam the scheduler's cron /
one-off paths use, G11.2-T2 #1096).

### Reconnect-with-backoff for the `LISTEN` connection

`_listen_for_notify` holds one engine connection for the lifetime of
the drain task. On any connection failure it silently degrades to
polling-only with no retry. Acceptable for v0.2 (the outbox is
durable via polling; tail latency reverts to the tick cadence rather
than sub-second) but a reconnect-with-backoff loop would protect
tail latency across PG restarts.

### Per-second `NOTIFY` cost at high producer throughput

Every `publish` registers a fresh `after_commit` listener that opens
a fresh engine connection. The `once=True` listener dedups per-commit,
but N producer transactions per second still cost N `engine.connect()`
round-trips. v0.2's anticipated "dozens per minute" volume keeps this
comfortable; revisit if a connector emits bursts.

## Settings

Both gated via env vars (defaults shown in parens):

- `EVENT_DRAIN_ENABLED` (`true`) — start the drain task at lifespan
  startup. `false` opts out cleanly (the start helper returns `None`
  and the lifespan shutdown tolerates that shape).
- `EVENT_DRAIN_TICK_INTERVAL_SECONDS` (`10`, range `[1, 3600]`) —
  bound between consecutive polled scans. The `NOTIFY` hint can cut
  the actual wake-up shorter than this interval; this bounds the
  *worst-case* poll latency when no producers fire and no listeners
  are connected.

## Test coverage

- [backend/tests/test_event_outbox.py](backend/tests/test_event_outbox.py)
  — producer + drain unit tests:
  - `test_publish_inserts_outbox_row_in_caller_transaction`
  - `test_publish_rolls_back_with_producer`
  - `test_succeed_run_publishes_outbox_event_in_same_tx`
  - `test_fail_run_publishes_outbox_event`
  - `test_terminal_event_rolls_back_with_run_transition`
  - `test_drain_no_double_process_under_concurrency` — two concurrent
    ticks, sum of processed == N exactly
  - `test_restart_durability_drains_unprocessed_rows` — publish →
    simulate kill → restart → tick drains the row
- [backend/tests/test_migration_0026_event_outbox.py](backend/tests/test_migration_0026_event_outbox.py)
  — schema + index migration round-trip

## References

- Goal #800 (G11 Agentic ops runtime)
- Initiative #804 (G11.3 Scheduler P2)
- This Task #824 — research note (transactional-outbox vs
  LISTEN/NOTIFY durability)
- Sibling Tasks: T1 #822 (substrate-decision spike), T2 #1065 (cron +
  one-off, merged), T4 #825 (in-flight resume), T5 #826 (admin
  surface — owns the subscription-matcher)
- Companion: [scheduler.md](scheduler.md) — the cron + one-off
  substrate this layer joins on
- PG `LISTEN`/`NOTIFY` durability:
  https://www.postgresql.org/docs/current/sql-notify.html
- `pg_try_advisory_lock`:
  https://www.postgresql.org/docs/current/functions-admin.html#FUNCTIONS-ADVISORY-LOCKS
- `FOR UPDATE SKIP LOCKED`:
  https://www.postgresql.org/docs/current/sql-select.html#SQL-FOR-UPDATE-SHARE
