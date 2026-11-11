"""One shared question: does an unfinished agent run already cover this work_ref?

Used by the checks investigator (one run per cause group) and by the event
trigger matcher (one run per ``coalesce_key``), so the two cannot drift.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentRun, AgentRunStatus

__all__ = ["NON_TERMINAL_STATUSES", "has_in_flight_run"]

#: The three unfinished ``agent_run`` statuses. A run in any of them for the
#: same ``(tenant_id, work_ref)`` blocks a duplicate fire (an
#: ``awaiting_approval`` run is still in flight -- do not re-fire it).
NON_TERMINAL_STATUSES: tuple[AgentRunStatus, ...] = (
    AgentRunStatus.PENDING,
    AgentRunStatus.RUNNING,
    AgentRunStatus.AWAITING_APPROVAL,
)


async def has_in_flight_run(tenant_id: uuid.UUID, work_ref: str) -> bool:
    """Return ``True`` iff an unfinished run already carries this work_ref.

    Rides ``agent_run_tenant_work_ref_idx`` on ``(tenant_id, work_ref)``.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = (
            select(AgentRun.id)
            .where(
                AgentRun.tenant_id == tenant_id,
                AgentRun.work_ref == work_ref,
                AgentRun.status.in_([s.value for s in NON_TERMINAL_STATUSES]),
            )
            .limit(1)
        )
        return (await session.execute(stmt)).first() is not None
