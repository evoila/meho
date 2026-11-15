# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Dispatch-time target-activity advisory (#2550, #3762).

After a write-class op succeeds, the dispatcher asks
:func:`build_target_activity_advisory` whether another agent holds an
active claim on the same target. If so, the op response carries a short
list of those claims under :data:`ADVISORY_EXTRAS_KEY`. It is a nudge,
never a lock or a block: no write ever waits for, or is refused because
of, a claim.

What it reads (#3762)
=====================

Only intentional claims from trusted agents (#3736), read from the
``agent_announcement`` archive. Automatic operation activity is not on
Broadcast any more (#3758) and is never part of the advisory; the audit
log is its record. A claim shows up when its newest step is inside the
look-back window and keeps the claim active
(:mod:`meho_backplane.broadcast.claim_state`): a trusted claim step,
not the ``completion``, with a finite TTL that has not run out.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Final

import structlog
from sqlalchemy import select

from meho_backplane.auth.delegation import resolve_actor_sub
from meho_backplane.auth.operator import Operator
from meho_backplane.broadcast.agent_events import AgentAnnouncementEvent
from meho_backplane.broadcast.claim_archive import CLAIM_STEPS_NEWEST_FIRST, completed_claim_ids
from meho_backplane.broadcast.claim_state import is_active_claim_step, latest_claim_steps
from meho_backplane.broadcast.history import (
    WRITE_OP_CLASSES,
    _target_matches,
    row_to_announcement,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentAnnouncement
from meho_backplane.settings import get_settings

__all__ = [
    "ADVISORY_EXTRAS_KEY",
    "build_target_activity_advisory",
]

_log = structlog.get_logger(__name__)

#: Maximum advisory entries surfaced on a single dispatch response. The
#: advisory is an awareness nudge, not an audit -- the caller who needs
#: the full picture reads ``meho_broadcast_recent``.
_ADVISORY_MAX_ENTRIES: Final[int] = 5

#: Most archive rows one advisory read loads, newest first. The window
#: setting already bounds the read in time; this caps the row count so a
#: burst of announcements cannot turn one dispatch into an unbounded
#: read. Newest first, so the cap keeps the claims that matter now.
_ADVISORY_SCAN_LIMIT: Final[int] = 100

#: The ``extras`` key the advisory rides on the :class:`OperationResult`.
ADVISORY_EXTRAS_KEY: Final[str] = "target_activity_advisory"


def _advisory_entry(claim: AgentAnnouncementEvent) -> dict[str, Any]:
    """Project the newest step of one claim onto the compact advisory shape.

    Server-stamped fields only: ``principal_sub``, the trusted
    ``owner_sub``, ``phase``, ``ts`` and ``expires_at``. Agent-authored
    free text (``activity`` / ``scope`` / ``target`` / ``targets``) is
    never copied, so the untrusted-prose envelope cannot enter an op
    response (Initiative #2543, review finding 27).
    """
    expires_at = claim.expires_at
    return {
        "principal_sub": claim.principal_sub,
        "owner_sub": claim.owner_sub,
        "kind": "announcement",
        "phase": claim.phase,
        "ts": claim.ts.isoformat(),
        "expires_at": None if expires_at is None else expires_at.isoformat(),
    }


async def _active_target_claims_newest_first(
    operator: Operator,
    *,
    target_name: str,
    window_minutes: int,
) -> list[AgentAnnouncementEvent]:
    """Return the newest step of each active claim on *target_name*, newest first.

    One bounded archive read of the tenant's trusted claim steps inside the
    window (``agent_announcement_tenant_created_at_idx``), then the claim
    rules run in-process on the stored values, never on the read tools'
    untrusted wrap. A claim with a stored ``completion`` is left out also
    when that completion is past the read's cap
    (:func:`~meho_backplane.broadcast.claim_archive.completed_claim_ids`).
    Propagates database errors to the caller's fail-open guard.
    """
    since = datetime.now(UTC) - timedelta(minutes=window_minutes)
    stmt = (
        select(AgentAnnouncement)
        .where(
            AgentAnnouncement.tenant_id == operator.tenant_id,
            AgentAnnouncement.created_at >= since,
            AgentAnnouncement.claim_id.is_not(None),
            AgentAnnouncement.owner_sub.is_not(None),
        )
        .order_by(*CLAIM_STEPS_NEWEST_FIRST)
        .limit(_ADVISORY_SCAN_LIMIT)
    )
    async with get_sessionmaker()() as session:
        rows = (await session.execute(stmt)).scalars().all()
        completed = await completed_claim_ids(
            session, operator.tenant_id, (row.claim_id for row in rows)
        )
    now = datetime.now(UTC)
    return [
        claim
        for claim in latest_claim_steps(row_to_announcement(row) for row in rows)
        if claim.claim_id not in completed
        and is_active_claim_step(claim, now)
        and _target_matches(claim, target_name)
    ]


async def build_target_activity_advisory(
    operator: Operator,
    *,
    op_class: str,
    target_name: str | None,
) -> dict[str, Any]:
    """Build the ``extras`` fragment naming other agents' active claims.

    Post-op awareness for a write-class dispatch (#2550): when the op
    changes a target that another agent has an active claim on, the
    response carries a compact advisory so the caller learns about the
    overlap at the moment it matters. This is NOT a lock or a block;
    checking before the op stays the discipline's ``meho_broadcast_recent``
    read step.

    Returns ``{"target_activity_advisory": [...]}`` with up to
    :data:`_ADVISORY_MAX_ENTRIES` of the newest peer claims in
    chronological order, or an empty dict (no key added) when the
    advisory does not apply. The empty-dict short-circuits, in order:

    * the feature is disabled (``dispatch_activity_advisory_window_minutes
      == 0``);
    * *op_class* is not write-class (read-class dispatches do no archive
      read). The dispatcher passes the class it stores on the op's audit
      row;
    * the dispatch has no target;
    * no other agent holds an active claim on the target.

    The caller's own claims are left out: a claim whose ``principal_sub``
    and ``actor_sub`` both match the caller. A sibling agent under the
    same person (another ``actor_sub``) is a peer and shows up.

    Fail-open and bounded: the success path awaits one count-capped
    archive read, and a broad guard swallows any error so the advisory
    lookup never turns a successful op into a failure.
    """
    window_minutes = get_settings().dispatch_activity_advisory_window_minutes
    if window_minutes <= 0:
        return {}
    if op_class not in WRITE_OP_CLASSES:
        return {}
    if not target_name:
        return {}

    caller_principal = operator.sub
    caller_actor = resolve_actor_sub()
    try:
        claims = await _active_target_claims_newest_first(
            operator, target_name=target_name, window_minutes=window_minutes
        )
    except Exception:
        # Advisory is best-effort awareness; any failure (a database
        # error, a parse bug) must never turn a successful dispatch into
        # a failure.
        _log.warning("target_activity_advisory_failed", tenant_id=str(operator.tenant_id))
        return {}

    peers = [
        claim
        for claim in claims
        if not (claim.principal_sub == caller_principal and claim.actor_sub == caller_actor)
    ]
    if not peers:
        return {}
    newest = peers[:_ADVISORY_MAX_ENTRIES]
    return {ADVISORY_EXTRAS_KEY: [_advisory_entry(claim) for claim in reversed(newest)]}
