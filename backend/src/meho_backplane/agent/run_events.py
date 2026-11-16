# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read the saved events of a hosted agent run.

A hosted run saves each event while it runs
(:mod:`meho_backplane.session_store.hosted_runs`). This module reads them
back for the run console and the run detail page.

Who sees what:

* Only runs of the caller's tenant. Another tenant's run is "not found".
* The data of an event follows the rule for the run's output
  (:func:`~meho_backplane.agent.invocation.run_output_visible_to`): a direct
  run acts for the person who started it, so only that person reads its
  event data. Others see the kind and the order of the events.
* An event that the secret check was not sure about (quarantined) shows its
  data only to a tenant admin (the ``read_quarantine`` right of the store).

A read with an audit ``path`` writes one audit row
(``sessions.run_events.read``) and commits it before the answer goes out.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select

from meho_backplane.agent.invocation import (
    AgentRunNotFoundError,
    _root_trigger,
    run_output_visible_to,
)
from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentRunStatus
from meho_backplane.operations import agent_run as run_lifecycle
from meho_backplane.session_store.audit import add_audit_row
from meho_backplane.session_store.hosted_runs import HOSTED_RUN_APP
from meho_backplane.session_store.models import AgentSessionEvent, AgentSessionRecord
from meho_backplane.session_store.rights import SessionRight, has_right

__all__ = ["RunEventsView", "StoredRunEvent", "read_run_events"]

_TERMINAL = frozenset(status.value for status in run_lifecycle.TERMINAL_STATUSES)


@dataclass(frozen=True, slots=True)
class StoredRunEvent:
    """One saved event as the caller may see it.

    ``data`` is empty when the event is ``withheld`` (the caller may not read
    the run's data) or ``quarantined`` (and the caller is not a tenant admin).
    """

    seq: int
    kind: str
    data: dict[str, Any]
    created_at: datetime
    quarantined: bool
    withheld: bool


@dataclass(frozen=True, slots=True)
class RunEventsView:
    """The saved events of one run after a ``seq``, and the run's state.

    ``state`` is the state of the run's session (``open``, ``ended`` or
    ``unfinished``), or ``None`` when the run has no saved session (a child
    run, or a run from before events were saved).
    """

    run_id: uuid.UUID
    run_status: str
    state: str | None
    events: tuple[StoredRunEvent, ...]

    @property
    def run_done(self) -> bool:
        """Whether the run reached its end status."""
        return self.run_status in _TERMINAL

    @property
    def awaiting_approval(self) -> bool:
        """Whether the run waits for a person to approve a step."""
        return self.run_status == AgentRunStatus.AWAITING_APPROVAL.value


async def read_run_events(
    operator: Operator,
    run_id: uuid.UUID,
    *,
    after: int = 0,
    limit: int | None = None,
    audit_path: str | None = None,
) -> RunEventsView:
    """Return the run's saved events with ``seq`` after *after*, in order.

    The run status is read before the events. So when the view says the run
    is done, it holds every event the run saved.

    Raises:
        AgentRunNotFoundError: no such run in the caller's tenant.
    """
    async with get_sessionmaker()() as session:
        row = await run_lifecycle.get_run(session, run_id)
        if row is None or row.tenant_id != operator.tenant_id:
            raise AgentRunNotFoundError(run_id)
        run_status = row.status
        show_data = run_output_visible_to(
            row, operator, root_trigger=await _root_trigger(session, row)
        )
        state = await session.scalar(
            select(AgentSessionRecord.state).where(
                AgentSessionRecord.id == run_id,
                AgentSessionRecord.tenant_id == operator.tenant_id,
                AgentSessionRecord.app == HOSTED_RUN_APP,
            )
        )
        query = (
            select(AgentSessionEvent)
            .where(
                AgentSessionEvent.record_id == run_id,
                AgentSessionEvent.tenant_id == operator.tenant_id,
                AgentSessionEvent.seq > after,
            )
            .order_by(AgentSessionEvent.seq)
        )
        rows = list(await session.scalars(query if limit is None else query.limit(limit)))
        if audit_path is not None:
            add_audit_row(
                session,
                operator,
                op_id="sessions.run_events.read",
                method="GET",
                path=audit_path,
                payload={"run_id": str(run_id), "after": after, "count": len(rows)},
            )
            await session.commit()
    may_open_quarantine = has_right(operator, SessionRight.READ_QUARANTINE)
    return RunEventsView(
        run_id=run_id,
        run_status=run_status,
        state=state,
        events=tuple(
            _visible(event, show_data=show_data, may_open_quarantine=may_open_quarantine)
            for event in rows
        ),
    )


def _visible(
    event: AgentSessionEvent, *, show_data: bool, may_open_quarantine: bool
) -> StoredRunEvent:
    withheld = not show_data
    hidden = withheld or (event.quarantined and not may_open_quarantine)
    return StoredRunEvent(
        seq=event.seq,
        kind=event.kind,
        data={} if hidden else dict(event.data),
        created_at=event.created_at,
        quarantined=event.quarantined,
        withheld=withheld,
    )
