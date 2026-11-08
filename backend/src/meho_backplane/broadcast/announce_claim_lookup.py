# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The caller's active-claim lookup (#4091).

Does the caller hold an active announcement that covers a write-class op,
and if not, why not? The reflex advisory's announce-before-mutate nudge
(:mod:`meho_backplane.broadcast.reflex`) asks this on every write from an
agent session, through :func:`announce_claim_gap`. The answer is advice
only; no write waits for a claim (#3762).

The lookup reads the ``agent_announcement`` archive, the same record
``meho_broadcast_recent`` reads. The index
``agent_announcement_tenant_principal_created_at_idx`` (migration ``0135``,
#5005) matches its filter, so the query reads only the caller's own rows.

Moved here from :mod:`meho_backplane.broadcast.history` (#5005); the
behaviour did not change.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final, Literal

from sqlalchemy import select

from meho_backplane.auth.operator import Operator
from meho_backplane.broadcast.agent_events import AgentAnnouncementEvent
from meho_backplane.broadcast.claim_state import latest_claim_steps
from meho_backplane.broadcast.history import _target_matches, row_to_announcement
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentAnnouncement

__all__ = [
    "ClaimGap",
    "announce_claim_gap",
]

#: Lookback for the caller's own active-claim lookup. Announcement TTLs are
#: bounded at 1440 minutes (``AgentAnnouncementEvent.ttl_minutes`` upper
#: bound), so a claim older than this window can no longer be active.
_CLAIM_SCAN_WINDOW_MINUTES: Final[int] = 1440

#: Most archive rows the claim lookup reads, newest first (#4091). The rows
#: are only the caller's own announcements of the last day, so normal use
#: stays far below this. The cap bounds the cost for a principal that
#: announces all day at the rate limit; such a principal does not see a
#: claim it made more than this many announcements ago.
_CLAIM_SCAN_LIMIT: Final[int] = 500

#: Why no active claim of the caller covers a write-class op (#4091). The
#: reflex advisory names the reason, so the agent knows what to fix:
#:
#: * ``no_claim`` -- the caller has no active announcement at all;
#: * ``other_target`` -- the caller's active announcements name other targets;
#: * ``other_op_class`` -- an active announcement names this target but
#:   declared another ``planned_op_class``;
#: * ``expired`` -- an announcement would cover the op, but its TTL ran out.
ClaimGap = Literal["no_claim", "other_target", "other_op_class", "expired"]

#: When the caller's announcements give different reasons, the one closest
#: to a covering claim wins: an expired claim for this exact op says more
#: than an active claim for another target.
_CLAIM_GAP_RANK: Final[dict[ClaimGap, int]] = {
    "no_claim": 0,
    "other_target": 1,
    "other_op_class": 2,
    "expired": 3,
}


def _claim_covers_target(event: AgentAnnouncementEvent, target_name: str | None) -> bool:
    """Whether an announce claim's target attribution covers the op's target.

    A claim that named no target (neither ``target`` nor ``targets``) is
    target-agnostic and covers any op. A target-less op
    (``target_name is None``) is covered by any claim. Otherwise the claim
    must name the op's target (exact match via :func:`_target_matches`).
    """
    claim_has_target = event.target is not None or bool(event.targets)
    if not claim_has_target or target_name is None:
        return True
    return _target_matches(event, target_name)


def _claim_gap(
    claim: AgentAnnouncementEvent,
    *,
    op_class: str,
    target_name: str | None,
    now: datetime,
) -> ClaimGap | None:
    """Why *claim* does not cover the op, or ``None`` when it does."""
    covers_target = _claim_covers_target(claim, target_name)
    covers_class = claim.planned_op_class is None or claim.planned_op_class == op_class
    expires_at = claim.expires_at
    expired = expires_at is None or expires_at <= now
    if covers_target and covers_class:
        return "expired" if expired else None
    if expired:
        return "no_claim"
    return "other_op_class" if covers_target else "other_target"


async def announce_claim_gap(
    operator: Operator,
    *,
    op_class: str,
    target_name: str | None,
) -> ClaimGap | None:
    """Return ``None`` when the caller holds an active claim covering this op.

    Otherwise return why not (:data:`ClaimGap`), for the #3133 reflex
    advisory's announce-before-mutate heuristic.

    Reads the caller's announcements of the last
    :data:`_CLAIM_SCAN_WINDOW_MINUTES` minutes from the
    ``agent_announcement`` archive (newest first, at most
    :data:`_CLAIM_SCAN_LIMIT` rows): the record ``meho_broadcast_recent``
    reads too. Not the Valkey stream (#4091): its write may fail, and a
    capped read of it drops a claim behind other principals' entries.

    Only trusted claims count (#3762): rows without a ``claim_id`` and
    ``owner_sub`` are history from before trusted admission. Each claim is
    judged by its newest step
    (:func:`~meho_backplane.broadcast.claim_state.latest_claim_steps`); a
    claim whose newest step is the ``completion`` is finished and counts
    as no claim.

    A claim covers the op when all hold:

    * it was published for this principal (``principal_sub ==
      operator.sub``, so a delegated agent's claim rides its human
      principal's ``sub``);
    * its finite TTL has not run out (a step without ``ttl_minutes``
      counts as expired);
    * its declared ``planned_op_class`` matches *op_class* (the class the
      dispatcher stores on the op's audit row), or the claim declared no class (a
      class-agnostic claim covers any op); and
    * its target attribution covers the op (see :func:`_claim_covers_target`),
      matched on the stored values, never on the read tools' untrusted wrap.

    Propagates database errors to the caller's fail-open guard (the reflex
    advisory answers ``{}``).
    """
    since = datetime.now(UTC) - timedelta(minutes=_CLAIM_SCAN_WINDOW_MINUTES)
    stmt = (
        select(AgentAnnouncement)
        .where(
            AgentAnnouncement.tenant_id == operator.tenant_id,
            AgentAnnouncement.principal_sub == operator.sub,
            AgentAnnouncement.created_at >= since,
            AgentAnnouncement.claim_id.is_not(None),
            AgentAnnouncement.owner_sub.is_not(None),
        )
        .order_by(AgentAnnouncement.created_at.desc(), AgentAnnouncement.seq.desc())
        .limit(_CLAIM_SCAN_LIMIT)
    )
    async with get_sessionmaker()() as session:
        rows = (await session.execute(stmt)).scalars().all()
    now = datetime.now(UTC)
    gap: ClaimGap = "no_claim"
    for claim in latest_claim_steps(row_to_announcement(row) for row in rows):
        if claim.phase == "completion":
            continue
        row_gap = _claim_gap(claim, op_class=op_class, target_name=target_name, now=now)
        if row_gap is None:
            return None
        if _CLAIM_GAP_RANK[row_gap] > _CLAIM_GAP_RANK[gap]:
            gap = row_gap
    return gap
