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
from meho_backplane.broadcast.client import get_broadcast_client
from meho_backplane.broadcast.events import BroadcastEvent
from meho_backplane.broadcast.history import row_to_announcement
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentAnnouncement

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
#: cadence per tenant fleet. No paired "errors" counter -- the
#: ``publish_agent_announcement`` entry point is **fail-loud** (distinct
#: from :func:`publish_event`'s fail-open contract), so a Valkey teardown
#: surfaces as a JSON-RPC ``-32603`` Internal Error to the calling
#: agent rather than as a silent metric increment. The
#: ``mcp_handler_error`` structlog event from the dispatcher's exception
#: handler is the operational signal on the failure path; the success
#: counter is the only metric this entry point emits.
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
    * ``cursor`` -- the Valkey stream entry id, or ``None`` on a replay
      (nothing new was written to the stream).
    * ``replayed`` -- ``True`` when the ``idempotency_key`` matched an
      earlier, identical step; no new row and no new stream entry.
    """

    event: AgentAnnouncementEvent
    cursor: str | None
    replayed: bool


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
    idempotency_key: str,
) -> AgentAnnouncementEvent | None:
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
    return stored


async def _check_open_claim(session: AsyncSession, event: AgentAnnouncementEvent) -> None:
    """Check an ``update`` / ``completion`` continues an open claim the caller owns.

    Legacy rows (no ``claim_id``) never match, so an old archive entry
    can never be continued as a claim.
    """
    start = (
        await session.execute(
            select(AgentAnnouncement.owner_sub, AgentAnnouncement.run_id).where(
                AgentAnnouncement.tenant_id == event.tenant_id,
                AgentAnnouncement.claim_id == event.claim_id,
                AgentAnnouncement.phase == "start",
            )
        )
    ).one_or_none()
    if start is None:
        raise AnnouncementClaimError(CLAIM_NOT_FOUND, "no claim with this claim_id in this tenant")
    if start.owner_sub != event.owner_sub or start.run_id != event.run_id:
        raise AnnouncementClaimError(
            CLAIM_NOT_OWNED, "only the claim's owner can update or complete it"
        )
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


def _announcement_row(
    event: AgentAnnouncementEvent, idempotency_key: str | None
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
    )


async def _persist_agent_announcement(
    event: AgentAnnouncementEvent,
    idempotency_key: str | None,
) -> AgentAnnouncementEvent | None:
    """Check the claim rules and write one durable :class:`AgentAnnouncement` row.

    Broadcast v2 T2 (#2547) made this row the durable archive, written
    **before** the ``XADD`` so a stream failure never costs the record.
    #3736 adds the claim rules, run in the same transaction as the insert:

    * a known ``idempotency_key`` for the same owner returns the stored
      step (a safe retry) instead of writing again; the same key for a
      different step is refused;
    * an ``update`` / ``completion`` must continue an open claim that the
      same owner (and the same hosted run) started.

    The partial unique indexes catch the race where two requests pass the
    checks at the same time: the loser re-reads and replays, or gets
    ``claim_completed``.

    Returns the stored event on a replay, else ``None`` (a new row was
    written). Any other DB error propagates (fail-loud).
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        if idempotency_key is not None:
            stored = await _find_by_idempotency_key(session, event, idempotency_key)
            if stored is not None:
                return stored
        if event.phase != "start":
            await _check_open_claim(session, event)
        session.add(_announcement_row(event, idempotency_key))
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            if idempotency_key is not None:
                stored = await _find_by_idempotency_key(session, event, idempotency_key)
                if stored is not None:
                    return stored
            if event.phase == "completion":
                raise AnnouncementClaimError(
                    CLAIM_COMPLETED, "this claim is already completed"
                ) from None
            raise
    return None


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
    """Fail-loud publish of an agent-authored announcement to the tenant stream.

    Companion to :func:`publish_event` -- same wire shape
    (``XADD meho:feed:{tenant_id}`` with a single ``event`` field
    carrying the JSON-serialised model), same ``MAXLEN ~`` trim
    semantics, same per-tenant stream key. The two differ on a single
    load-bearing axis: failure semantics.

    Fail-loud vs. fail-open
    -----------------------

    :func:`publish_event` swallows every exception silently because the
    audit row is canonical and a request-path failure to log a
    side-channel broadcast is worse than the operator missing one entry
    in the feed. **Inverted contract here.**

    An agent-authored announcement is the AGENT's deliberate
    communication to its operators. A swallowed announcement leaves the
    agent thinking it told the team while the team never saw it -- the
    opposite of the team-coordination property the broadcast discipline
    is meant to provide. The right semantics is:

    1. Publisher raises on any redis-py / Valkey failure.
    2. The MCP dispatcher (:mod:`meho_backplane.mcp.server`) catches the
       exception and surfaces it as JSON-RPC ``-32603`` Internal Error.
    3. The calling agent sees the error and can retry, reroute, or
       degrade gracefully -- it KNOWS the announcement didn't land.

    The same wire-side stream contract (one ``event`` JSON field per
    entry) means T1's :func:`meho_backplane.mcp.tools.broadcast.broadcast_recent`
    surfaces both kinds back to readers via the same ``XRANGE`` -- T1's
    parser dispatches on the ``event_kind`` field to pick the right
    model class.

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

    Returns
    -------
    AnnouncementPublishResult
        The stored event, the Valkey stream entry id (``"<ms>-<seq>"``)
        the ``XADD`` wrote (``None`` on a replay), and whether the call
        was an idempotent replay.

    Raises
    ------
    ValueError
        *event* is not a trusted claim (no ``claim_id`` / ``owner_sub``).
    AnnouncementClaimError
        A claim rule failed (unknown claim, not the owner, already
        completed, reused idempotency key).
    Exception
        Anything :func:`get_broadcast_client` or :meth:`Redis.xadd`
        raises propagates verbatim. The dispatcher's generic exception
        handler maps it to JSON-RPC ``-32603``; the log line
        (``mcp_handler_error``) carries the exception class for
        operator triage. No log line is emitted here -- adding one
        would shadow the dispatcher's structured exception log and
        violate the "one log line per failure" discipline the chassis
        observability guide enforces.
    """
    if not event.is_trusted_claim:
        # Only events built from a checked agent identity may be
        # published (#3736); see build_trusted_announcement.
        raise ValueError("publish_agent_announcement needs a trusted claim (claim_id + owner_sub)")
    # Durable archive first (T2 #2547): the DB row is the coordination
    # window that survives a Valkey restart. Written before the XADD so a
    # stream-side failure never costs the durable record. Fail-loud --
    # any DB exception propagates to the -32603 path.
    stored = await _persist_agent_announcement(event, idempotency_key)
    if stored is not None:
        return AnnouncementPublishResult(event=stored, cursor=None, replayed=True)
    client = get_broadcast_client()
    entry_id = await client.xadd(
        _stream_key(event.tenant_id),
        {"event": event.model_dump_json()},
        maxlen=BROADCAST_MAXLEN,
        approximate=True,
    )
    BROADCAST_AGENT_ANNOUNCEMENTS_TOTAL.labels(phase=event.phase).inc()
    # ``decode_responses=True`` on the broadcast client (see
    # :mod:`~meho_backplane.broadcast.client`) makes xadd's return value
    # ``str``; the cast is documentary rather than runtime-meaningful but
    # keeps mypy honest about the public contract this function advertises.
    return AnnouncementPublishResult(event=event, cursor=str(entry_id), replayed=False)
