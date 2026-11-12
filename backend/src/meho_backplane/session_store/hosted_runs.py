# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hosted agent runs keep their events in the session store.

A hosted run is an agent run that MEHO starts itself. Each one gets one
session in the store:

* :func:`add_run_session` adds its ``agent_session_record`` row in the
  transaction that creates the run. The row's id and ``session_id`` are the
  run id, the project is the agent name, and the app is
  :data:`HOSTED_RUN_APP`.
* :class:`RunEventLog` saves each event of the run (``turn``,
  ``tool_call``, ``tool_result``, ``final`` or ``error``) as one
  ``agent_session_event`` row, in order, when it happens. Each event passes
  the same secret check as an upload (:func:`check_event`). A ``final`` or
  ``error`` event sets the session state to ``ended``.
* :func:`end_run_session` runs when the run reaches its end state (it
  succeeded, failed or was cancelled). It writes the outcome. A session
  that is still ``open`` then becomes ``unfinished``: the run stopped
  without a final event (a crash, a restart, a lost lease or a cancel).
  MEHO never adds an ending that the run did not make.

The event rows are in the database, not in the object storage. So a run
saves its events also when no object storage is set up, and a reader can
follow a live run in order.
"""

from __future__ import annotations

import asyncio
import functools
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import structlog
from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.session_store.models import AgentSessionEvent, AgentSessionRecord
from meho_backplane.session_store.secret_check import check_part

if TYPE_CHECKING:
    from meho_backplane.agent.run import AgentRunEvent

__all__ = [
    "END_KINDS",
    "HOSTED_RUN_APP",
    "CheckedEvent",
    "RunEventLog",
    "add_run_session",
    "check_event",
    "end_run_session",
]

_log = structlog.get_logger(__name__)

#: Saves in flight. A strong reference, so a save that outlives a cancelled
#: run is not garbage-collected before it commits.
_SAVES: set[asyncio.Future[None]] = set()

#: The ``app`` of the session record of a hosted run.
HOSTED_RUN_APP: Final[str] = "meho-agent-run"

#: The event kinds that end a run. Saving one sets the session to ``ended``.
END_KINDS: Final[frozenset[str]] = frozenset({"final", "error"})


@dataclass(frozen=True, slots=True)
class CheckedEvent:
    """The data of one event after the secret check."""

    data: dict[str, Any]
    quarantined: bool
    redactions: int


def check_event(data: dict[str, Any]) -> CheckedEvent:
    """Run the secret check of the session store on the data of one event.

    The data is written as one JSON line, so the check reads every string
    in it (also a JSON text inside a string). Known secrets are replaced
    with a marker. When the check is not sure, the event is marked as
    quarantined: it is saved with the known secrets replaced, and only a
    tenant admin can read it.
    """
    line = json.dumps(data, separators=(",", ":"), default=str)
    part = check_part(line.encode("utf-8"))
    checked = json.loads(part.content.decode("utf-8"))
    return CheckedEvent(
        data=checked if isinstance(checked, dict) else {"value": checked},
        quarantined=part.unclear,
        redactions=sum(part.redactions.values()),
    )


def add_run_session(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    tenant_id: uuid.UUID,
    started_by: str,
    agent_name: str,
) -> None:
    """Add the session record of a new hosted run to *session* (the caller commits)."""
    now = datetime.now(UTC)
    session.add(
        AgentSessionRecord(
            id=run_id,
            tenant_id=tenant_id,
            session_id=str(run_id),
            uploaded_by_sub=started_by,
            project=agent_name,
            app=HOSTED_RUN_APP,
            app_version=None,
            started_at=now,
            ended_at=None,
            state="open",
            outcome=None,
            files={},
            upload_count=0,
            created_at=now,
            last_seen_at=now,
        )
    )


async def end_run_session(session: AsyncSession, *, run_id: uuid.UUID, outcome: str) -> None:
    """Write the end of a hosted run's session in *session* (the caller commits).

    *outcome* is the run's end status. A session that is still ``open`` has
    no final event, so it becomes ``unfinished``. A run with no session
    record (a child run, or a run from before this feature) is left alone.
    """
    now = datetime.now(UTC)
    await session.execute(
        update(AgentSessionRecord)
        .where(AgentSessionRecord.id == run_id, AgentSessionRecord.app == HOSTED_RUN_APP)
        .values(
            state=case(
                (AgentSessionRecord.state == "open", "unfinished"),
                else_=AgentSessionRecord.state,
            ),
            outcome=outcome,
            ended_at=now,
            last_seen_at=now,
        )
        .execution_options(synchronize_session=False)
    )


class RunEventLog:
    """Save the events of one hosted run, in order, when they happen.

    One run has one writer, so the order of :meth:`save` calls is the order
    of ``seq``. Each event commits on its own, so a reader sees it at once
    and a crash keeps every event saved before it.

    A save that fails is logged, and the log stops saving: the saved events
    then end before the failure and never have a gap. The run itself goes
    on. Its session ends ``unfinished`` unless a final event was saved.
    """

    def __init__(self, run_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
        self.run_id = run_id
        self.tenant_id = tenant_id
        self._next_seq: int | None = None
        self._stopped = False

    async def save(self, kind: str, data: dict[str, Any]) -> None:
        """Check and save one event. Never raises, except on cancel.

        The save runs in its own task, shielded from a cancel of the run: a
        save that has started always ends with a commit or a rollback, so a
        cancel never leaves a database transaction open. The event happened,
        so it is kept.
        """
        if self._stopped:
            return
        task = asyncio.ensure_future(self._save(kind, data))
        _SAVES.add(task)
        task.add_done_callback(_SAVES.discard)
        task.add_done_callback(functools.partial(self._note_failure, kind))
        try:
            await asyncio.shield(task)
        except Exception:  # logged by _note_failure
            self._stopped = True

    def _note_failure(self, kind: str, task: asyncio.Future[None]) -> None:
        if task.cancelled() or task.exception() is None:
            return
        self._stopped = True
        _log.warning(
            "hosted_run_event_not_saved",
            run_id=str(self.run_id),
            kind=kind,
            error_type=type(task.exception()).__name__,
        )

    async def save_event(self, event: AgentRunEvent) -> None:
        """Check and save one event of the agent loop (:meth:`save`)."""
        await self.save(event.kind.value, event.data)

    async def _save(self, kind: str, data: dict[str, Any]) -> None:
        checked = check_event(data)
        now = datetime.now(UTC)
        async with get_sessionmaker()() as session:
            if self._next_seq is None:
                self._next_seq = await self._first_seq(session)
                if self._next_seq is None:
                    self._stopped = True
                    return
            session.add(
                AgentSessionEvent(
                    record_id=self.run_id,
                    seq=self._next_seq,
                    tenant_id=self.tenant_id,
                    kind=kind,
                    data=checked.data,
                    quarantined=checked.quarantined,
                    redactions=checked.redactions,
                    created_at=now,
                )
            )
            if kind in END_KINDS:
                await session.execute(
                    update(AgentSessionRecord)
                    .where(AgentSessionRecord.id == self.run_id, AgentSessionRecord.state == "open")
                    .values(state="ended", last_seen_at=now)
                    .execution_options(synchronize_session=False)
                )
            await session.commit()
        self._next_seq += 1

    async def _first_seq(self, session: AsyncSession) -> int | None:
        """Return the next free ``seq``, or ``None`` when the run has no session."""
        record = await session.scalar(
            select(AgentSessionRecord.id).where(
                AgentSessionRecord.id == self.run_id,
                AgentSessionRecord.tenant_id == self.tenant_id,
                AgentSessionRecord.app == HOSTED_RUN_APP,
            )
        )
        if record is None:
            return None
        last = await session.scalar(
            select(func.max(AgentSessionEvent.seq)).where(
                AgentSessionEvent.record_id == self.run_id
            )
        )
        return (last or 0) + 1
