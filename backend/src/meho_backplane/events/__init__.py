# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Events package -- transactional outbox + drain (G11.3-T3 #824).

Initiative #804 (G11.3 Scheduler P2), Task #824 (T3). The
third trigger shape for the scheduler: an agent run fires in response
to a MEHO-internal event (an audit predicate match, a connector
alert, or another agent run reaching a terminal state).

Per the issue's research note: plain PostgreSQL ``LISTEN/NOTIFY`` is
**not durable** -- a notification sent while no listener is connected
is lost forever. The durable, replica-safe shape is the
**transactional outbox**: producers ``INSERT`` an
:class:`~meho_backplane.db.models.EventOutbox` row in the same
transaction that writes the event-producing state change. A separate
drain loop scans the outbox via ``SELECT ... FOR UPDATE SKIP LOCKED``,
claims unprocessed rows, dispatches them, and marks them processed.

``LISTEN/NOTIFY`` is layered on top as a **latency hint** only: the
producer's same-transaction commit triggers an asynchronous NOTIFY
that wakes the drain loop's sleep early, dropping per-event latency
from "next 5-10s tick" to "sub-second". A dropped notification is
benign -- the next polled tick picks the row up anyway.

The subscription matcher (:mod:`meho_backplane.events.matcher`) fires
every active ``kind='event'``
:class:`~meho_backplane.db.models.ScheduledTrigger` whose
``event_filter`` the drained ``event_outbox.payload`` contains
(``payload @> event_filter``), through the same
``AgentInvoker.run_scheduled`` seam the cron / one-off scheduler uses.
Each fire is deduped per (event, trigger) on the run's work_ref, so a
redelivered event never double-fires an agent.

Public surface
==============

* :func:`publish` -- producer-side writer; insert an outbox row in the
  caller's open session (same-transaction discipline).
* :func:`start_event_drain` / :func:`stop_event_drain` -- lifespan
  helpers main.py wires.
"""

from meho_backplane.events.drain import start_event_drain, stop_event_drain
from meho_backplane.events.outbox import publish

__all__ = [
    "publish",
    "start_event_drain",
    "stop_event_drain",
]
