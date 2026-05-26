# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Producer-side outbox writer + post-commit NOTIFY hint (G11.3-T3 #824).

Initiative #804 (G11.3 Scheduler P2), Task #824 (T3). Producers call
:func:`publish` inside their existing SQLAlchemy session so the
event-outbox row commits in the **same transaction** as the
event-producing state change. The same-transaction discipline is what
makes the outbox durable: a producer commit that hits the DB but
crashes before the in-memory event could be dispatched still has the
``event_outbox`` row in place; the drain loop will pick it up on its
next tick.

Post-commit ``NOTIFY``
======================

The drain loop on the same DB ``LISTEN``s on
:data:`~meho_backplane.db.models.EVENT_OUTBOX_NOTIFY_CHANNEL` so a
freshly-inserted event can wake the loop's sleep early (sub-second
latency vs. the 5-10s tick). The notification is **not durable** -- if
no listener is connected the notification is lost -- but that's fine
because the durable channel is the outbox row itself; ``NOTIFY`` is a
latency hint, not a delivery mechanism.

The chosen wiring is **post-commit**: the producer registers a
SQLAlchemy ``after_commit`` event-listener on the session at the
:func:`publish` call site so the ``NOTIFY`` fires only after the
insert has actually committed. A pre-commit ``NOTIFY`` would wake the
drain before the outbox row is visible to its read transaction
(``READ COMMITTED`` isolation), which would burn a wake-up cycle and
add latency rather than removing it.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import EVENT_OUTBOX_NOTIFY_CHANNEL, EventOutbox

__all__ = ["publish"]


def _register_post_commit_notify(session: AsyncSession) -> None:
    """Attach a one-shot ``after_commit`` listener that fires ``NOTIFY``.

    SQLAlchemy ``Session`` events are sync; the AsyncSession exposes
    its sync wrapper via :attr:`AsyncSession.sync_session`. We attach
    a ``once=True`` listener so a session that publishes multiple
    events still notifies only once per commit (de-duplication is
    cheap and avoids notify-storms when a batched producer writes N
    outbox rows in one transaction).

    The listener body runs synchronously after commit on the
    sync_session; it submits a ``NOTIFY`` statement on a *fresh*
    short-lived connection via the bound engine. We deliberately do
    not reuse the just-committed connection because (a) ``NOTIFY`` is
    advisory and benign if it fails, and (b) the producer's connection
    may already be returned to the pool by the time the listener
    fires (autocommit-style FastAPI session dependency).
    """

    sync_session = session.sync_session
    # SQLAlchemy fires this listener after the session-level commit;
    # if the publish was inside an outer transaction (``async with
    # session.begin():``) the trigger lands at the outer commit.

    bind = sync_session.bind
    # We only wire NOTIFY through a top-level :class:`Engine` -- a bare
    # :class:`Connection` (the rare session-bound-to-connection shape)
    # has no engine to checkout from, so skip the hint and let polling
    # carry the load. ``None`` means a detached session -- the unit-
    # test write-only fixture shape; same skip rule.
    if not isinstance(bind, Engine):
        return
    engine: Engine = bind
    dialect_name: str = engine.dialect.name

    @event.listens_for(sync_session, "after_commit", once=True)
    def _emit_notify(_session: Any) -> None:
        # Only PG supports NOTIFY; SQLite (the unit-test path) has no
        # such mechanism, so the hint is silently skipped. This is the
        # same dialect-gate the scheduler's advisory-lock path uses.
        if dialect_name != "postgresql":
            return
        try:
            # Fresh short-lived connection -- the producer's
            # original connection may already be returned to the pool
            # (FastAPI session dependency) by the time after_commit
            # fires. NOTIFY is fire-and-forget, so we open + close
            # immediately.
            with engine.connect() as conn:
                conn.execute(
                    text(f"NOTIFY {EVENT_OUTBOX_NOTIFY_CHANNEL}"),
                )
                conn.commit()
        except Exception:
            # NOTIFY is a latency hint, not a durability mechanism;
            # never let a failed wake-hint roll back a successful
            # outbox commit. The drain's polling cadence picks up the
            # row anyway.
            pass


async def publish(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    event_kind: str,
    payload: dict[str, object] | None = None,
) -> EventOutbox:
    """Append one ``event_outbox`` row in *session*'s open transaction.

    Same-transaction discipline (this Task's load-bearing invariant):
    the outbox INSERT shares the producer's commit. A producer
    rollback (the agent-run transition failed) discards the outbox
    row alongside; a producer commit makes the outbox row durable.
    The drain loop's next tick (or the post-commit ``NOTIFY`` hint,
    whichever fires first) picks the row up.

    Args:
        session: Open :class:`AsyncSession` the producer already owns.
            The function flushes (so ``event_id`` is populated) but
            does not commit -- the caller's transaction owns the
            commit.
        tenant_id: The tenant the event belongs to (real FK to
            ``tenant.id``).
        event_kind: Discriminator the subscription matcher uses
            (e.g. ``agent_run.completed``). Free-text; see the model
            docstring for why this is not a closed enum.
        payload: Event-specific data the subscriber's filter matches
            against. ``None`` is normalised to ``{}`` so the
            ``NOT NULL`` column always carries a valid JSON object.

    Returns:
        The inserted, flushed :class:`EventOutbox` row.
    """
    row = EventOutbox(
        tenant_id=tenant_id,
        event_kind=event_kind,
        payload=payload if payload is not None else {},
    )
    session.add(row)
    await session.flush()
    # Validate payload is JSON-serialisable at publish time rather
    # than at drain time -- catches the bug at the producer call
    # site (where the dev can fix it) instead of in the drain logs.
    # JSONB stores natively so this is cheap; the round-trip catches
    # objects (datetimes, UUIDs) that the serialiser would refuse.
    try:
        json.dumps(row.payload, sort_keys=True, default=str)
    except (TypeError, ValueError) as exc:  # pragma: no cover -- defensive
        raise ValueError(
            f"event_outbox payload is not JSON-serialisable: {exc}",
        ) from exc
    _register_post_commit_notify(session)
    return row
