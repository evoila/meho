# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Dispatch-time target-activity advisory (#2550).

After a write-class op succeeds, the dispatcher asks
:func:`build_target_activity_advisory` whether other principals were
recently active on the same target. If so, the op response carries a short
list of that activity under :data:`ADVISORY_EXTRAS_KEY`. It is a nudge,
never a lock or a block.

The advisory reads the per-tenant Valkey stream with the shared read
primitives of :mod:`meho_backplane.broadcast.history`
(:func:`~meho_backplane.broadcast.history.parse_entry`,
:func:`~meho_backplane.broadcast.history.event_matches`).

Moved here from :mod:`meho_backplane.broadcast.history` (#5005); the
behaviour did not change.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast

import structlog

from meho_backplane.auth.delegation import resolve_actor_sub
from meho_backplane.auth.operator import Operator
from meho_backplane.broadcast.agent_events import AgentAnnouncementEvent
from meho_backplane.broadcast.client import get_broadcast_client
from meho_backplane.broadcast.events import BroadcastEvent, classify_op
from meho_backplane.broadcast.history import (
    WRITE_OP_CLASSES,
    event_matches,
    parse_entry,
    stream_key,
)
from meho_backplane.settings import get_settings

__all__ = [
    "ADVISORY_EXTRAS_KEY",
    "build_target_activity_advisory",
]

_log = structlog.get_logger(__name__)

#: ``XREVRANGE`` upper anchor. ``"+"`` is Valkey's "latest available
#: entry" sentinel.
_XRANGE_END: Final[str] = "+"

#: op_class values that carry the dispatch-time target-activity advisory
#: (#2550). Only mutating classes qualify -- a write is where crossfire
#: matters. Read-class dispatches skip the stream read entirely, keeping
#: the hot read path free of the lookup. Aliases :data:`WRITE_OP_CLASSES`
#: so the #2550 gate and the #3133 features stay in lockstep.
_ADVISORY_WRITE_OP_CLASSES: Final[frozenset[str]] = WRITE_OP_CLASSES

#: Maximum advisory entries surfaced on a single dispatch response. The
#: advisory is an awareness nudge, not an audit -- the caller who needs
#: the full picture queries ``meho_broadcast_recent``.
_ADVISORY_MAX_ENTRIES: Final[int] = 5

#: Bounded scan cap for the advisory stream read. The window (settings
#: knob) already bounds the range in time; this caps the entry count so
#: a burst inside the window can't turn one dispatch into an unbounded
#: read. The read is newest-first (``XREVRANGE``), so this cap keeps the
#: NEWEST entries in the window -- the ones the advisory cares about --
#: rather than clipping them off the tail (the busy-target crossfire case
#: an oldest-first ``XRANGE`` + ``COUNT`` would silently invert).
_ADVISORY_SCAN_LIMIT: Final[int] = 100

#: The ``extras`` key the advisory rides on the :class:`OperationResult`.
ADVISORY_EXTRAS_KEY: Final[str] = "target_activity_advisory"


def _advisory_entry(event: BroadcastEvent | AgentAnnouncementEvent) -> dict[str, Any]:
    """Project one parsed stream event onto the compact advisory shape.

    Structured, server-derived fields ONLY -- ``principal_sub`` /
    ``actor_sub`` (lineage), ``kind``, the operation's ``op_id`` or the
    announcement's ``phase``, and ``ts`` (ISO-8601). Read straight off the
    parsed model, so agent-authored free text (``activity`` / ``scope`` /
    ``target`` / ``targets``) is never even touched: the untrusted-prose
    envelope cannot enter an op response (Initiative #2543, review finding
    27). An :class:`AgentAnnouncementEvent` is labelled ``announcement``;
    a :class:`BroadcastEvent` is an ``operation``.
    """
    if isinstance(event, AgentAnnouncementEvent):
        return {
            "principal_sub": event.principal_sub,
            "kind": "announcement",
            "phase": event.phase,
            "ts": event.ts.isoformat(),
        }
    entry: dict[str, Any] = {
        "principal_sub": event.principal_sub,
        "kind": "operation",
        "op_id": event.op_id,
        "ts": event.ts.isoformat(),
    }
    if event.actor_sub is not None:
        entry["actor_sub"] = event.actor_sub
    return entry


async def _recent_target_events_newest_first(
    operator: Operator,
    *,
    target_name: str,
    window_minutes: int,
    limit: int,
) -> list[BroadcastEvent | AgentAnnouncementEvent]:
    """Read active, target-matched events in the window, newest first.

    A single ``XREVRANGE meho:feed:{tenant} + <since-ms>`` capped at
    *limit*: reverse order means the ``COUNT`` cap keeps the NEWEST
    entries in the window, so the advisory samples the tail (recent
    crossfire) rather than the head. Target and ``active_only`` filtering
    run in-process on the parsed model -- the same
    :func:`event_matches` narrowing every other reader uses, before any
    prose wrap. Propagates :class:`redis.exceptions.RedisError` to the
    caller's fail-open guard.
    """
    client = get_broadcast_client()
    key = stream_key(operator.tenant_id)
    since_ms = max(
        int((datetime.now(UTC) - timedelta(minutes=window_minutes)).timestamp() * 1000), 0
    )
    raw_entries = cast(
        "list[tuple[str, dict[str, str]]]",
        await client.xrevrange(key, max=_XRANGE_END, min=str(since_ms), count=limit),
    )
    now = datetime.now(UTC)
    events: list[BroadcastEvent | AgentAnnouncementEvent] = []
    for entry_id, fields in raw_entries:
        event = parse_entry(entry_id, fields, stream_key=key)
        if event is None:
            continue
        if event_matches(
            event, op_class=None, principal=None, target=target_name, active_only=True, now=now
        ):
            events.append(event)
    return events


async def build_target_activity_advisory(
    operator: Operator,
    *,
    op_id: str,
    target_name: str | None,
) -> dict[str, Any]:
    """Build the ``extras`` fragment advising of recent peer activity.

    Post-op awareness for a write-class dispatch (#2550): when the op
    mutates a target that another principal has touched recently -- an
    operation or an active announcement claim -- the response carries a
    compact advisory so the caller learns about the overlap at the moment
    it matters. This is NOT a lock or a block; pre-op checking remains the
    discipline's ``meho_broadcast_recent`` read step.

    Returns ``{"target_activity_advisory": [...]}`` with up to
    :data:`_ADVISORY_MAX_ENTRIES` most-recent peer entries in chronological
    order, or an empty dict (no key added) when the advisory does not
    apply. The empty-dict short-circuits, in order:

    * the feature is disabled (``dispatch_activity_advisory_window_minutes
      == 0``);
    * the op is not write-class (read-class dispatches perform no stream
      read -- the frozenset check returns before any Valkey call);
    * the dispatch has no target;
    * no peer activity was found in the window.

    Fail-open and bounded: the success path awaits a single, count-capped
    ``XREVRANGE`` (a small, bounded cost -- not free), and a broad guard
    swallows any error (a Valkey teardown included) so the advisory lookup
    never converts a successful op into a failure. Because the read is
    newest-first, the ``COUNT`` cap keeps the newest window entries, so
    ``[-_ADVISORY_MAX_ENTRIES:]`` after re-chronologising is the genuinely
    most-recent peer activity even on a target emitting more than
    :data:`_ADVISORY_SCAN_LIMIT` events in the window.
    """
    window_minutes = get_settings().dispatch_activity_advisory_window_minutes
    if window_minutes <= 0:
        return {}
    if classify_op(op_id) not in _ADVISORY_WRITE_OP_CLASSES:
        return {}
    if not target_name:
        return {}

    caller_principal = operator.sub
    caller_actor = resolve_actor_sub()
    try:
        events = await _recent_target_events_newest_first(
            operator,
            target_name=target_name,
            window_minutes=window_minutes,
            limit=_ADVISORY_SCAN_LIMIT,
        )
    except Exception:
        # Advisory is best-effort awareness; any failure (a Valkey
        # teardown, a parse bug) must never convert a successful dispatch
        # into a failure.
        _log.warning(
            "target_activity_advisory_failed",
            stream_key=stream_key(operator.tenant_id),
        )
        return {}

    # ``events`` is newest-first; drop the caller's own activity, keep the
    # newest _ADVISORY_MAX_ENTRIES, then restore chronological order.
    peers = [
        event
        for event in events
        if not (
            event.principal_sub == caller_principal
            and getattr(event, "actor_sub", None) == caller_actor
        )
    ]
    if not peers:
        return {}
    newest = peers[:_ADVISORY_MAX_ENTRIES]
    return {ADVISORY_EXTRAS_KEY: [_advisory_entry(event) for event in reversed(newest)]}
