# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read the state of announcement claims from the archive (#3762).

The newest step of a claim says where the claim stands
(:mod:`meho_backplane.broadcast.claim_state`). "Newest" means committed
last. ``agent_announcement.seq`` follows commit order
(:mod:`meho_backplane.broadcast.archive_sequence`). Timestamps do not:
a step gets its time before the write takes the tenant's lock, and two
servers can have different clocks.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Final, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import UnaryExpression

from meho_backplane.broadcast.claim_state import is_active_claim_step
from meho_backplane.broadcast.history import row_to_announcement
from meho_backplane.db.models import AgentAnnouncement

__all__ = ["CLAIM_STEPS_NEWEST_FIRST", "ended_claim_ids"]

#: Newest committed step first. A row a pod on the older release wrote has
#: no number yet. It takes the next number on the next scan, so it counts
#: as newer than every numbered row. A ``completion`` still ends its claim
#: when such a row sorts above it
#: (:func:`~meho_backplane.broadcast.claim_state.latest_claim_steps`,
#: :func:`ended_claim_ids`).
CLAIM_STEPS_NEWEST_FIRST: Final[tuple[UnaryExpression[int | None], ...]] = (
    AgentAnnouncement.seq.desc().nulls_first(),
)


async def ended_claim_ids(
    session: AsyncSession, tenant_id: UUID, steps: Sequence[AgentAnnouncement]
) -> frozenset[UUID]:
    """Return the claims of *steps* that have ended.

    A claim has ended when it has a stored ``completion``, or when its
    newest step does not keep it active (its TTL has run out). Reads past
    *steps*, because these steps can be outside the page. Every *steps*
    row must have its ``seq``.

    The completion is looked up by claim alone, not by ``seq``: a start
    an older release wrote without a number gets its number later, so it
    can have a higher ``seq`` than the completion that ended it. For the
    TTL check, the newest step of a claim is never below the lowest
    ``seq`` of *steps*, which bounds that read.
    """
    claimed = [step for step in steps if step.claim_id is not None]
    if not claimed:
        return frozenset()
    claim_ids = {step.claim_id for step in claimed}
    completed = await session.execute(
        select(AgentAnnouncement.claim_id).where(
            AgentAnnouncement.tenant_id == tenant_id,
            AgentAnnouncement.claim_id.in_(claim_ids),
            AgentAnnouncement.phase == "completion",
        )
    )
    newest_seqs = (
        select(func.max(AgentAnnouncement.seq))
        .where(
            AgentAnnouncement.tenant_id == tenant_id,
            AgentAnnouncement.seq >= min(cast("int", step.seq) for step in claimed),
            AgentAnnouncement.claim_id.in_(claim_ids),
        )
        .group_by(AgentAnnouncement.claim_id)
    )
    newest = await session.execute(
        select(AgentAnnouncement).where(
            AgentAnnouncement.tenant_id == tenant_id,
            AgentAnnouncement.seq.in_(newest_seqs),
        )
    )
    now = datetime.now(UTC)
    expired = {
        cast("UUID", row.claim_id)
        for row in newest.scalars()
        if not is_active_claim_step(row_to_announcement(row), now)
    }
    return frozenset(expired | {cast("UUID", claim_id) for claim_id in completed.scalars()})
