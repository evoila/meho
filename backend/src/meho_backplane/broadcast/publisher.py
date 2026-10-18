# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Fail-open publish-on-write to the per-tenant Valkey stream (G6.1-T3).

Constructs one ``XADD meho:feed:{tenant_id}`` per audited operation,
trimmed to :data:`BROADCAST_MAXLEN` via ``MAXLEN ~`` (approximate).
Two Prometheus counters surface the publish path on ``/metrics``:

* :data:`BROADCAST_EVENTS_PUBLISHED_TOTAL` — labelled by ``op_class``
  + ``result_status`` so operators can see the broadcast surface
  partitioned by sensitivity class and outcome.
* :data:`BROADCAST_PUBLISH_ERRORS_TOTAL` — unlabelled, counts every
  fail-open swallow (Valkey unreachable, ``XADD`` rejected, redis-py
  client teardown race). Operators alert off this counter; the
  cardinality is intentionally coarse.

Fail-open semantics
===================

A publish failure NEVER propagates back to the audit-middleware or
MCP handler call site. Reasons:

* The audit row is the canonical record; the broadcast feed is the
  real-time view. Returning a 500 to the operator because the broadcast
  side-channel didn't reach Slack would be worse than the operator just
  missing one event in the feed.
* Valkey unreachability is operationally common (rolling restart of
  the broadcast subchart, brief network blips); making it a request-path
  failure would multiply the operational cost of every Valkey wobble
  by the chassis's QPS.

The trade-off is **at-most-once** publish semantics — events can be
dropped, never duplicated. Subscribers that need at-least-once
ordering query :class:`~meho_backplane.db.models.AuditLog` by
``audit_id`` (the field on every :class:`BroadcastEvent`) to reconcile
gaps. G8's audit-query API (#334) is the upstream path for this.

References
----------

* Valkey ``XADD``: https://valkey.io/commands/xadd/
* Decision #3 (PII defaults the publisher relies on the upstream
  :func:`~meho_backplane.broadcast.events.redact_payload` to enforce):
  ``docs/decisions/locked-decisions.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

import structlog
from prometheus_client import Counter
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.broadcast.agent_events import AgentAnnouncementEvent
from meho_backplane.broadcast.announcement_claims import (
    CLAIM_COMPLETED,
    CLAIM_NOT_FOUND,
    CLAIM_NOT_OWNED,
    IDEMPOTENCY_KEY_REUSED,
    AnnouncementClaimError,
)
from meho_backplane.broadcast.archive_sequence import next_announcement_seq
from meho_backplane.broadcast.client import get_broadcast_client
from meho_backplane.broadcast.events import BroadcastEvent
from meho_backplane.broadcast.history import row_to_announcement
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentAnnouncement
from meho_backplane.settings import get_settings

__all__ = [
    "BROADCAST_AGENT_ANNOUNCEMENTS_TOTAL",
    "BROADCAST_EVENTS_PUBLISHED_TOTAL",
    "BROADCAST_MAXLEN",
    "BROADCAST_PUBLISH_ERRORS_TOTAL",
    "AnnouncementPublishResult",
    "publish_agent_announcement",
    "publish_event",
]


#: Approximate ceiling on entries kept per ``meho:feed:{tenant_id}``
#: stream. Heuristic per the G6.1-T3 task body: 10k entries ≈ 24h at
#: moderate load. The actual retention window is operator-tunable via
#: :attr:`~meho_backplane.settings.Settings.broadcast_retention_hours`;
#: a future refactor (post-T6) may derive this constant from that
#: setting and an events-per-hour estimate. v0.2 ships the static
#: ceiling — keeps the publish hot path one ``XADD`` with constant
#: kwargs.
BROADCAST_MAXLEN: Final[int] = 10000


#: Per-event publish success counter. The two labels partition the
#: feed so operators can graph "broadcast volume by op class" without
#: fanning into per-tenant cardinality. ``op_class`` matches the
#: ``BroadcastEvent.op_class`` taxonomy from
#: :func:`~meho_backplane.broadcast.events.classify_op`;
#: ``result_status`` matches the ``"ok"`` / ``"error"`` / ``"denied"``
#: trichotomy the handlers produce.
BROADCAST_EVENTS_PUBLISHED_TOTAL: Counter = Counter(
    "broadcast_events_published_total",
    "BroadcastEvents successfully XADD'd to the per-tenant Valkey stream.",
    labelnames=("op_class", "result_status"),
)

#: Per-failure counter for the fail-open swallow path. Unlabelled by
#: design: the failure modes (Valkey unreachable, XADD-rejected, redis-py
#: teardown race) all reduce to "the broadcast feed missed one event",
#: and an alert on a sustained nonzero rate is the only operational
#: signal that matters. Cardinality is intentionally coarse — adding
#: ``error_class`` as a label would multiply series by every redis-py
#: exception subclass the worker happens to see.
BROADCAST_PUBLISH_ERRORS_TOTAL: Counter = Counter(
    "broadcast_publish_errors_total",
    "BroadcastEvent publishes that failed (Valkey unreachable / XADD error / teardown race).",
)


#: Per-phase counter for agent-authored announcement publishes (G6.4-T2
#: #1092). Labelled by ``phase`` (``"start"`` / ``"update"`` /
#: ``"completion"``) so dashboards can graph the announce-discipline
#: cadence per tenant fleet. Counts stream entries written. An archive
#: failure surfaces to the agent as JSON-RPC ``-32603``; a stream failure
#: after the archive commit returns ``delivery_pending`` and logs
#: ``broadcast_announcement_delivery_pending`` (#3748).
BROADCAST_AGENT_ANNOUNCEMENTS_TOTAL: Counter = Counter(
    "broadcast_agent_announcements_total",
    "Agent-authored AgentAnnouncementEvent publishes (G6.4-T2 fail-loud entry).",
    labelnames=("phase",),
)


_log = structlog.get_logger(__name__)


def _stream_key(tenant_id: UUID) -> str:
    """Build the per-tenant Valkey Streams key.

    Centralised here so a future tenancy-isolation tightening (e.g.
    a per-environment prefix) lands in one place rather than at every
    publisher call site.
    """
    return f"meho:feed:{tenant_id}"


@dataclass(frozen=True)
class AnnouncementPublishResult:
    """Outcome of :func:`publish_agent_announcement`.

    * ``event`` -- the stored announcement. On a replay this is the
      first stored copy, so the caller echoes the original ids.
    * ``seq`` -- the stored row's archive sequence number (#3748). Readers
      page and watch by it. ``None`` only for a row an older MEHO version
      wrote without a number during a rolling upgrade.
    * ``stream_entry_id`` -- the Valkey stream entry id this call wrote,
      or ``None`` when it wrote none (the entry was already on the
      stream, or the stream write failed).
    * ``replayed`` -- ``True`` when the ``idempotency_key`` matched an
      earlier, identical step, so no new row was written.
    * ``delivery_pending`` -- ``True`` when the row is committed to the
      archive but the stream write failed. The announcement is stored and
      readers see it (they read the archive); only the wake-up for live
      watchers is missing. A retry with the same ``idempotency_key``
      writes the stream entry without a second row.
    """

    event: AgentAnnouncementEvent
    seq: int | None
    stream_entry_id: str | None
    replayed: bool
    delivery_pending: bool = False


@dataclass(frozen=True)
class _StoredStep:
    """A stored step: found by its retry key, or just written.

    ``delivered`` says whether it reached the stream; ``replayed`` whether
    it was found by its retry key rather than written by this call.
    """

    event: AgentAnnouncementEvent
    delivered: bool
    seq: int | None
    replayed: bool = True


_STEP_FIELDS = (
    "run_id",
    "phase",
    "activity",
    "target",
    "targets",
    "scope",
    "planned_op_class",
    "ttl_minutes",
    "work_ref",
)


def _same_step(stored: AgentAnnouncementEvent, event: AgentAnnouncementEvent) -> bool:
    """Whether a retry with the same key asks for the same step.

    The key is scoped per owner, so every field the caller sends must
    match, including the hosted run: a second run of the same agent that
    reuses a key must not get the first run's claim back.
    """
    if any(getattr(stored, f) != getattr(event, f) for f in _STEP_FIELDS):
        return False
    return event.phase == "start" or stored.claim_id == event.claim_id


async def _find_by_idempotency_key(
    session: AsyncSession,
    event: AgentAnnouncementEvent,
    idempotency_key: str | None,
) -> _StoredStep | None:
    if idempotency_key is None:
        return None
    row = (
        await session.execute(
            select(AgentAnnouncement).where(
                AgentAnnouncement.tenant_id == event.tenant_id,
                AgentAnnouncement.owner_sub == event.owner_sub,
                AgentAnnouncement.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    stored = row_to_announcement(row)
    if not _same_step(stored, event):
        raise AnnouncementClaimError(
            IDEMPOTENCY_KEY_REUSED,
            "this idempotency_key was already used for a different step",
        )
    return _StoredStep(event=stored, delivered=row.stream_entry_id is not None, seq=row.seq)


async def _check_open_claim(
    session: AsyncSession,
    event: AgentAnnouncementEvent,
    idempotency_key: str | None,
) -> _StoredStep | None:
    """Check an ``update`` / ``completion`` continues an open claim the caller owns.

    Legacy rows (no ``claim_id``) never match, so an old archive entry
    can never be continued as a claim.

    The claim's ``start`` row is locked (``SELECT ... FOR UPDATE``) until
    the caller's transaction ends. Two steps on one claim therefore run
    one after the other: an ``update`` that waits behind a ``completion``
    re-reads after the completion commits, sees its row, and gets
    ``claim_completed``. Without the lock both could pass this check and
    the update could commit after the completion. SQLite (dev/test only)
    has no row locks and drops the clause; the partial unique indexes
    still stop a second ``completion`` there.

    The retry key is looked up only after the lock is held. A retry that
    waited behind the first copy of the same step then finds that copy
    and replays it, instead of being refused with ``claim_completed``
    because the first copy closed the claim meanwhile. Returns the stored
    step on such a replay, else ``None``.
    """
    start = (
        await session.execute(
            select(AgentAnnouncement.owner_sub, AgentAnnouncement.run_id)
            .where(
                AgentAnnouncement.tenant_id == event.tenant_id,
                AgentAnnouncement.claim_id == event.claim_id,
                AgentAnnouncement.phase == "start",
            )
            .with_for_update()
        )
    ).one_or_none()
    if start is None:
        raise AnnouncementClaimError(CLAIM_NOT_FOUND, "no claim with this claim_id in this tenant")
    if start.owner_sub != event.owner_sub or start.run_id != event.run_id:
        raise AnnouncementClaimError(
            CLAIM_NOT_OWNED, "only the claim's owner can update or complete it"
        )
    stored = await _find_by_idempotency_key(session, event, idempotency_key)
    if stored is not None:
        return stored
    completed = (
        await session.execute(
            select(AgentAnnouncement.id).where(
                AgentAnnouncement.tenant_id == event.tenant_id,
                AgentAnnouncement.claim_id == event.claim_id,
                AgentAnnouncement.phase == "completion",
            )
        )
    ).first()
    if completed is not None:
        raise AnnouncementClaimError(CLAIM_COMPLETED, "this claim is already completed")
    return None


def _announcement_row(
    event: AgentAnnouncementEvent, idempotency_key: str | None, seq: int
) -> AgentAnnouncement:
    return AgentAnnouncement(
        id=event.event_id,
        tenant_id=event.tenant_id,
        principal_sub=event.principal_sub,
        activity=event.activity,
        target=event.target,
        scope=event.scope,
        targets=list(event.targets),
        phase=event.phase,
        planned_op_class=event.planned_op_class,
        ttl_minutes=event.ttl_minutes,
        work_ref=event.work_ref,
        run_id=event.run_id,
        created_at=event.ts,
        claim_id=event.claim_id,
        owner_sub=event.owner_sub,
        actor_sub=event.actor_sub,
        admission=event.admission,
        idempotency_key=idempotency_key,
        seq=seq,
    )


async def _persist_agent_announcement(
    event: AgentAnnouncementEvent,
    idempotency_key: str | None,
) -> _StoredStep:
    """Check the claim rules and write one durable :class:`AgentAnnouncement` row.

    Broadcast v2 T2 (#2547) made this row the durable archive, written
    **before** the ``XADD`` so a stream failure never costs the record.
    #3736 adds the claim rules, run in the same transaction as the insert:

    * a known ``idempotency_key`` for the same owner returns the stored
      step (a safe retry) instead of writing again; the same key for a
      different step is refused;
    * an ``update`` / ``completion`` must continue an open claim that the
      same owner (and the same hosted run) started. The claim's start row
      is locked for the rest of the transaction (:func:`_check_open_claim`),
      so steps on one claim never interleave, and the retry key is looked
      up under that lock.

    A ``start`` has no row to lock, so two starts with the same key can
    pass the key lookup at the same time. The partial unique index then
    refuses the second insert; the loser re-reads and replays the winner.
    Delivery to the stream is serialised separately, per stored row
    (:func:`_deliver`).

    #3748: the new row takes the tenant's next archive sequence number
    (:func:`~meho_backplane.broadcast.archive_sequence.next_announcement_seq`)
    right before the INSERT. That locks the tenant's counter row until
    this transaction ends, so numbers follow commit order. A rollback,
    including the lost start race above, gives the number back.

    Returns the stored step: the replayed one, or the row just written
    (``replayed=False``). Any other DB error propagates (fail-loud).
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        if event.phase == "start":
            stored = await _find_by_idempotency_key(session, event, idempotency_key)
        else:
            stored = await _check_open_claim(session, event, idempotency_key)
        if stored is not None:
            return stored
        seq = await next_announcement_seq(session, event.tenant_id)
        session.add(_announcement_row(event, idempotency_key, seq))
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            stored = await _find_by_idempotency_key(session, event, idempotency_key)
            if stored is not None:
                return stored
            if event.phase == "completion":
                raise AnnouncementClaimError(
                    CLAIM_COMPLETED, "this claim is already completed"
                ) from None
            raise
    return _StoredStep(event=event, delivered=False, seq=seq, replayed=False)


#: Valkey-side "write this announcement once" step, run as one Lua script
#: so the check, the ``XADD`` and the marker write cannot interleave with
#: another request or be split by a lost reply. ``KEYS[1]`` is the tenant
#: stream, ``KEYS[2]`` the per-event delivery marker
#: (:func:`_delivery_marker_key`). ``ARGV`` is the event JSON, the
#: ``MAXLEN ~`` ceiling and the marker TTL in seconds. Returns
#: ``[1, entry_id]`` when this call wrote the entry and ``[0, entry_id]``
#: when the marker already held the id of an earlier write.
_DELIVER_ONCE_LUA: Final[str] = """
local known = redis.call('GET', KEYS[2])
if known then
  return {0, known}
end
local entry_id = redis.call('XADD', KEYS[1], 'MAXLEN', '~', ARGV[2], '*', 'event', ARGV[1])
redis.call('SET', KEYS[2], entry_id, 'EX', ARGV[3])
return {1, entry_id}
"""


def _delivery_marker_key(event: AgentAnnouncementEvent) -> str:
    """Key that remembers the stream entry id written for one announcement.

    Own namespace, separate from the ``meho:feed:{tenant}`` stream and the
    ``meho:ratelimit:announce:...`` counters.
    """
    return f"meho:announce:delivered:{event.tenant_id}:{event.event_id}"


async def _write_stream_entry_once(event: AgentAnnouncementEvent) -> tuple[bool, str]:
    """``XADD`` one announcement to its tenant stream unless it is already there.

    Runs :data:`_DELIVER_ONCE_LUA`. The per-event marker is what makes a
    retry safe when the row could not be marked after a successful write:
    the delivery-mark UPDATE failed, or the ``XADD`` ran on the server but
    its reply never reached this process. In both cases the archive row
    still says "not delivered", and without the marker the retry would
    write the same step to the stream a second time.

    The marker lives as long as the stream read window
    (``broadcast_retention_hours``); a retry after that is outside the
    window readers use anyway.

    Returns ``(written, entry_id)``: ``written`` is ``False`` when the
    marker already held the id of an earlier write. Fail-loud: any
    redis-py / Valkey error propagates to the caller.
    """
    client = get_broadcast_client()
    deliver_once = client.register_script(_DELIVER_ONCE_LUA)
    written, entry_id = await deliver_once(
        keys=[_stream_key(event.tenant_id), _delivery_marker_key(event)],
        args=[
            event.model_dump_json(),
            BROADCAST_MAXLEN,
            get_settings().broadcast_retention_hours * 3600,
        ],
    )
    # ``decode_responses=True`` on the broadcast client (see
    # :mod:`~meho_backplane.broadcast.client`) makes the entry id ``str``;
    # the casts are documentary rather than runtime-meaningful but keep
    # mypy honest about the public contract this module advertises.
    return bool(written), str(entry_id)


@dataclass(frozen=True)
class _Delivery:
    """What :func:`_deliver` did: the entry id it wrote, or that it failed."""

    written_entry_id: str | None
    pending: bool = False


async def _deliver(event: AgentAnnouncementEvent) -> _Delivery:
    """Write the stored step to the stream once and record the entry id on its row.

    Two guards keep one stored step at one stream entry:

    * The archive row is locked (``SELECT ... FOR UPDATE``) for the whole
      delivery: the delivery check, the stream write, and the UPDATE that
      sets ``stream_entry_id`` (the one UPDATE the application runs
      against ``agent_announcement``). Two requests for the same stored
      step, for example a retry that overlaps the first try, therefore
      deliver one after the other: the second waits, re-reads the row,
      finds the entry id, and writes nothing. The lock is held across one
      Valkey round trip, bounded by the broadcast client's 5 s socket
      timeout.
    * The stream write itself is :func:`_write_stream_entry_once`. When
      the first try wrote the entry but the row was never marked (the
      UPDATE failed, or the reply to the write was lost), the row lock
      cannot help: the next request reads "not delivered" again. The
      per-event marker on the Valkey side then returns the earlier entry
      id instead of writing a second entry, and this call records it on
      the row.

    Returns the entry id this call wrote (``None`` when the step was
    already on the stream). A failed stream write does not raise (#3748):
    the archive row is committed and is what readers read, so the call
    reports ``pending=True`` instead. The transaction rolls back, the row
    keeps ``stream_entry_id = NULL``, and a retry with the same key can
    finish the delivery. The UPDATE after a successful write is fail-soft
    too: the announcement is already on the stream and in the archive, so
    a failure there must not turn a delivered announcement into an error
    for the caller. The cost is one warning line; a later retry marks the
    row without writing again.
    """
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(AgentAnnouncement)
                .where(AgentAnnouncement.id == event.event_id)
                .with_for_update()
            )
        ).scalar_one()
        if row.stream_entry_id is not None:
            return _Delivery(written_entry_id=None)
        try:
            written, entry_id = await _write_stream_entry_once(event)
        except Exception as exc:
            await session.rollback()
            _log.warning(
                "broadcast_announcement_delivery_pending",
                error_class=type(exc).__name__,
                event_id=str(event.event_id),
            )
            return _Delivery(written_entry_id=None, pending=True)
        row.stream_entry_id = entry_id
        try:
            await session.commit()
        except Exception as exc:
            await session.rollback()
            _log.warning(
                "broadcast_announcement_delivery_mark_failed",
                error_class=type(exc).__name__,
                event_id=str(event.event_id),
            )
        return _Delivery(written_entry_id=entry_id if written else None)


async def publish_event(event: BroadcastEvent) -> None:
    """Fail-open publish to the per-tenant Valkey stream.

    Exactly one ``XADD`` per call:

    * ``key`` — ``meho:feed:{event.tenant_id}`` (per-tenant isolation).
    * ``fields`` — single ``event`` field carrying the JSON-serialised
      :class:`BroadcastEvent`. Single field rather than splitting the
      event into one Streams field per :class:`BroadcastEvent` attribute
      because the wire shape (one JSON blob) round-trips identically
      through SSE (T4), the MCP resource (T6), and any future Slack
      mirror (G6.2) — every consumer deserialises back to a
      :class:`BroadcastEvent`.
    * ``maxlen=BROADCAST_MAXLEN`` + ``approximate=True`` — the ``MAXLEN ~``
      form per the Valkey docs. Best-effort retention is acceptable for
      the broadcast feed; strict ``MAXLEN`` would force a slow per-call
      O(n) trim and the broadcast subscribers are already designed
      around at-most-once delivery (see :doc:`module docstring`).

    Failure handling: any exception from :func:`get_broadcast_client`
    or :meth:`Redis.xadd` is logged at warning level with the
    exception class only (no error message — redis-py exceptions can
    embed URL substrings that would leak the broadcast endpoint to
    log shippers), the error counter is incremented, and the call
    returns silently. The caller (AuditMiddleware on the HTTP path,
    MCP handlers on the JSON-RPC path) MUST NOT depend on this
    succeeding — the audit row is the canonical record.

    Parameters
    ----------
    event:
        The frozen :class:`BroadcastEvent`. Its ``payload`` field MUST
        already be the redacted view per
        :func:`~meho_backplane.broadcast.events.redact_payload`; this
        publisher does not re-redact, doesn't inspect the payload, and
        doesn't enforce the PII contract. See the events module
        docstring for why the contract is enforced upstream.
    """
    try:
        client = get_broadcast_client()
        await client.xadd(
            _stream_key(event.tenant_id),
            {"event": event.model_dump_json()},
            maxlen=BROADCAST_MAXLEN,
            approximate=True,
        )
    except Exception as exc:
        _log.warning(
            "broadcast_publish_failed",
            error_class=type(exc).__name__,
            tenant_id=str(event.tenant_id),
            op_id=event.op_id,
        )
        BROADCAST_PUBLISH_ERRORS_TOTAL.inc()
        return
    BROADCAST_EVENTS_PUBLISHED_TOTAL.labels(
        op_class=event.op_class,
        result_status=event.result_status,
    ).inc()


async def publish_agent_announcement(
    event: AgentAnnouncementEvent,
    *,
    idempotency_key: str | None = None,
) -> AnnouncementPublishResult:
    """Store an agent-authored announcement in the archive, then wake the stream.

    Companion to :func:`publish_event` -- same wire shape on the stream
    (``XADD meho:feed:{tenant_id}`` with a single ``event`` field carrying
    the JSON-serialised model), same ``MAXLEN ~`` trim, same per-tenant
    stream key.

    Archive first, stream second (#3748)
    ------------------------------------

    The ``agent_announcement`` row is the record. Readers
    (``meho_broadcast_recent`` / ``meho_broadcast_watch``) read the
    archive by its per-tenant sequence number; the stream entry only wakes
    live watchers early. So the two legs fail differently:

    * The archive write is **fail-loud**. Any DB error propagates; the MCP
      dispatcher maps it to JSON-RPC ``-32603`` and the agent knows the
      announcement did not land.
    * The stream write is **fail-soft**. When it fails after the row
      committed, the call still succeeds with ``delivery_pending=True``:
      the announcement is stored and readers see it, and watchers find it
      on their next archive scan. A retry with the same
      ``idempotency_key`` writes the stream entry and never a second row.

    Parameters
    ----------
    event:
        The frozen :class:`AgentAnnouncementEvent` to publish. Its
        ``tenant_id`` MUST be the operator's verified JWT-bound tenant;
        the handler enforces this structurally (no input field could
        ever ask for another tenant's stream).

    idempotency_key:
        The caller's retry key for this step (#3736). Required by the
        callers for ``update`` / ``completion``; optional for ``start``.
        A retry with the same key that finds the stored row without a
        stream entry id finishes the delivery with the stored copy's ids
        (see :func:`_deliver`): it writes the entry if the first try never
        reached the stream, or only marks the row if it did. A ``start``
        sent without a key cannot be retried this way: its row stays in
        the archive and the agent's retry opens a new claim.

    Returns
    -------
    AnnouncementPublishResult
        The stored event, its archive sequence number, the Valkey stream
        entry id this call wrote (``None`` when it wrote none), whether
        the call was an idempotent replay, and whether the stream wake-up
        is still pending.

    Raises
    ------
    ValueError
        *event* is not a trusted claim (no ``claim_id`` / ``owner_sub``).
    AnnouncementClaimError
        A claim rule failed (unknown claim, not the owner, already
        completed, reused idempotency key).
    Exception
        Any archive (DB) error propagates verbatim; the dispatcher maps it
        to JSON-RPC ``-32603``.
    """
    if not event.is_trusted_claim:
        # Only events built from a checked agent identity may be
        # published (#3736); see build_trusted_announcement.
        raise ValueError("publish_agent_announcement needs a trusted claim (claim_id + owner_sub)")
    stored = await _persist_agent_announcement(event, idempotency_key)
    if stored.delivered:
        return AnnouncementPublishResult(
            event=stored.event, seq=stored.seq, stream_entry_id=None, replayed=True
        )
    # A new row, or a replay whose row was stored but never marked as
    # delivered. _deliver makes sure the step reaches the stream once
    # (row lock + per-event marker), with the stored copy's ids.
    delivery = await _deliver(stored.event)
    if delivery.written_entry_id is not None:
        BROADCAST_AGENT_ANNOUNCEMENTS_TOTAL.labels(phase=stored.event.phase).inc()
    return AnnouncementPublishResult(
        event=stored.event,
        seq=stored.seq,
        stream_entry_id=delivery.written_entry_id,
        replayed=stored.replayed,
        delivery_pending=delivery.pending,
    )
