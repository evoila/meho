# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Scheduler package -- cron + one-off triggers fire agent runs (G11.3-T2).

Initiative #804 (G11.3 Scheduler, P2), Task #823 (T2). Two of the three
P2 trigger shapes (the third, event-subscription, is T3 #824 and lands
as a transactional outbox in a sibling package):

* **Cron triggers** -- fire repeatedly on a 5-field cron expression
  evaluated in the trigger's persisted timezone.
* **One-off triggers** -- fire once at a stored ``next_fire_at``, then
  the row transitions to ``status='fired'`` (terminal).

Both shapes share one durable row
(:class:`~meho_backplane.db.models.ScheduledTrigger`, migration 0018)
and one tick loop (:mod:`meho_backplane.scheduler.loop`). The loop
claims due rows replica-safely under
``pg_try_advisory_lock`` + ``SELECT ... FOR UPDATE SKIP LOCKED`` (PG;
no-op on the SQLite test path) and invokes the agent via the G11.1-T4
:class:`~meho_backplane.agent.invocation.AgentInvoker`.

Why a roll-our-own asyncio loop and not APScheduler / Celery / DBOS
==================================================================

Same posture as :mod:`meho_backplane.topology.scheduler` and
:mod:`meho_backplane.memory.expiry`: APScheduler 4.x has never shipped
a stable release (perpetual alpha; "should NOT be used in production"
per its own maintainer); Celery / arq / Temporal would each introduce
a new runtime substrate (broker daemons, worker pools) the chassis's
"no new substrate / minimal dependencies" discipline (``CLAUDE.md``)
rejects. DBOS Transact is the G11.3-T1 (#822) spike's alternative; T2
ships the roll-our-own default and the row shape is substrate-neutral
so a future DBOS rebase swaps only :mod:`scheduler.loop`.

The one new dependency is ``croniter`` -- pure-Python, single-purpose,
small surface area, MIT licensed -- which only handles the
cron-expression parse and the ``next_fire_at`` arithmetic. The actual
scheduling loop is hand-rolled stdlib ``asyncio``.

Replica-safety
==============

In a multi-replica deployment two backplane pods share one Postgres. A
naive "scan due rows + fire" loop would double-fire every trigger. The
loop guards on two layers:

1. **Process-wide ``pg_try_advisory_lock``** -- only one replica's
   loop runs the tick body at a time. Non-blocking: a replica that
   loses the race skips this tick and tries next cadence. Mirrors the
   precedent :mod:`topology.scheduler` set.
2. **Per-row ``SELECT ... FOR UPDATE SKIP LOCKED``** -- belt and
   braces in case the advisory-lock claim ever changes shape; ensures
   two concurrent in-process readers within the same replica (and
   across replicas during the brief window of advisory-lock acquisition
   ordering) never select the same row.

On SQLite (the unit-test path) both layers no-op. The test suite runs
two loop instances in the same process to assert single-fire under
contention without spinning up two Postgres containers; the
:func:`~meho_backplane.scheduler.repository.claim_due_triggers`
helper's in-process behaviour (``UPDATE ... WHERE next_fire_at <=
:now AND status='active'`` returning the affected ids) is the
single-fire enforcement on the test path.

Restart durability
==================

State lives in the ``scheduled_trigger`` row. A pod restart loses the
in-memory loop but **not** the durable next-fire timestamps. On
restart:

* A cron trigger whose ``next_fire_at`` has already passed fires once
  on the next tick and then advances to the next cron match (the
  "fire once on catch-up; do not storm" semantic the AC requires).
* A one-off trigger whose ``next_fire_at`` has already passed fires
  once on the next tick and transitions to ``fired``.

The loop deliberately does **not** replay every missed tick during a
long outage -- a 24-hour outage on a ``*/5 * * * *`` trigger would
otherwise fire 288 runs in a burst. One catch-up fire + a clean
re-anchor of ``next_fire_at`` is the consumer-doc-accepted shape.

Public surface
==============

The package exports the lifecycle helpers main.py wires:

* :func:`start_scheduler` -- start the loop as an asyncio task.
* :func:`stop_scheduler` -- cancel + await unwind.
"""

from meho_backplane.scheduler.loop import (
    start_scheduler,
    stop_scheduler,
)

__all__ = [
    "start_scheduler",
    "stop_scheduler",
]
