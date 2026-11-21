# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Link audit rows to the stored session they belong to (#4904).

Two kinds of session get a link:

* **A hosted agent run.** Its tool calls write audit rows with
  ``agent_session_id`` = the run id, and the run's session record has the
  run id as its id. So no extra row is needed.
* **An uploaded session.** :func:`link_mcp_sessions` runs at each upload
  with new bytes. For every MCP tool call in the new tails it looks for
  the audit row of that call: same tenant, same person, an MCP tool call
  row (``method = 'MCP'``), the same tool name and the same arguments hash,
  inside the session's time. A call that rows of more than one MCP session
  match (two sessions of the same person ran ``meho_status`` at the same
  time) says nothing about which one is this session, so it is skipped.
  Each ``agent_session_id`` left (the MCP session id) is stored as one
  :class:`AgentSessionLink` row, with the newest work reference (ticket id)
  of its rows. A session with no start time gets no link: without it the
  time window has no start.

A row whose session id is the CLI login session
(:data:`~meho_backplane.operations._audit.SESSION_FROM_CLIENT_KEY`) gets
no link: one login session spans many agent sessions.

:func:`stored_session_for` answers the read side. Only a caller that
:func:`may_read_sessions` may see the link; for any other caller the read
routes do not call it, so they give no hint that a session exists.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import MACHINE_PRINCIPAL_KINDS, Operator
from meho_backplane.db.models import AuditLog
from meho_backplane.operations._audit import agent_session_rows, payload_session_from_client
from meho_backplane.operations._validate import compute_audit_params_hash
from meho_backplane.session_store.hosted_runs import HOSTED_RUN_APP
from meho_backplane.session_store.models import AgentSessionLink, AgentSessionRecord
from meho_backplane.session_store.rights import SessionRight, has_right
from meho_backplane.session_store.tool_calls import ToolCall

__all__ = [
    "StoredSessionLink",
    "link_mcp_sessions",
    "may_read_sessions",
    "stored_session_for",
]

#: Clock difference allowed between the laptop's session times and MEHO's
#: audit times.
_TIME_SLACK: Final[timedelta] = timedelta(minutes=5)

#: The ``method`` of the audit row of an MCP tool call.
_MCP_METHOD: Final[str] = "MCP"


@dataclass(frozen=True)
class StoredSessionLink:
    """The stored session an audit row belongs to."""

    session_id: str
    """The id to read the session with (``meho sessions show <id>``)."""
    hosted_run: bool
    """True when the session is a hosted agent run; its id is the run id."""


def may_read_sessions(operator: Operator) -> bool:
    """Whether *operator* may see a link to a stored session.

    The same rule as ``GET /api/v1/sessions/{session_id}``: a person with
    the ``read`` right of the session store.
    """
    return operator.principal_kind not in MACHINE_PRINCIPAL_KINDS and has_right(
        operator, SessionRight.READ
    )


def _wanted(tool_calls: Sequence[ToolCall]) -> set[tuple[str, str]]:
    return {(tool, compute_audit_params_hash(arguments)) for tool, arguments in tool_calls}


async def _matching_rows(
    session: AsyncSession,
    record: AgentSessionRecord,
    wanted: set[tuple[str, str]],
    started_at: datetime,
    now: datetime,
) -> list[Any]:
    params_hash = AuditLog.payload["params_hash"].as_string()
    op_id = AuditLog.payload["op_id"].as_string()
    conditions = [
        AuditLog.tenant_id == record.tenant_id,
        AuditLog.operator_sub == record.uploaded_by_sub,
        AuditLog.method == _MCP_METHOD,
        AuditLog.agent_session_id.is_not(None),
        params_hash.in_(sorted({digest for _, digest in wanted})),
        agent_session_rows(),
        AuditLog.occurred_at >= started_at - _TIME_SLACK,
        # A session that has not ended yet ends at the latest at this upload.
        AuditLog.occurred_at <= (record.ended_at or now) + _TIME_SLACK,
    ]
    result = await session.execute(
        select(AuditLog.agent_session_id, op_id, params_hash, AuditLog.work_ref)
        .where(*conditions)
        .order_by(AuditLog.occurred_at)
    )
    return [row for row in result.all() if (row[1], row[2]) in wanted]


async def link_mcp_sessions(
    session: AsyncSession,
    record: AgentSessionRecord,
    tool_calls: Sequence[ToolCall],
    *,
    now: datetime,
) -> int:
    """Store the MCP sessions of *tool_calls* for *record*; the caller commits.

    Returns how many new MCP sessions were linked.
    """
    if not tool_calls or record.started_at is None:
        return 0
    # No flush here: a new record is inserted at the caller's commit, which
    # turns a lost race for the first upload into a clean 409.
    with session.no_autoflush:
        rows = await _matching_rows(session, record, _wanted(tool_calls), record.started_at, now)
        sessions_of_call: dict[tuple[str, str], set[uuid.UUID]] = defaultdict(set)
        for mcp_session_id, op_id, params_hash, _work_ref in rows:
            sessions_of_call[(op_id, params_hash)].add(mcp_session_id)
        # Only a call that rows of one MCP session match names the session.
        named = {next(iter(ids)) for ids in sessions_of_call.values() if len(ids) == 1}
        work_refs: dict[uuid.UUID, str | None] = {}
        for mcp_session_id, _op_id, _hash, work_ref in rows:
            if mcp_session_id in named:
                # Rows come oldest first, so the newest work reference wins.
                work_refs[mcp_session_id] = work_ref or work_refs.get(mcp_session_id)
        if not work_refs:
            return 0
        existing = {
            link.mcp_session_id: link
            for link in await session.scalars(
                select(AgentSessionLink).where(AgentSessionLink.record_id == record.id)
            )
        }
    added = 0
    for mcp_session_id, work_ref in work_refs.items():
        link = existing.get(mcp_session_id)
        if link is not None:
            if work_ref is not None:
                link.work_ref = work_ref
            continue
        session.add(
            AgentSessionLink(
                record_id=record.id,
                mcp_session_id=mcp_session_id,
                tenant_id=record.tenant_id,
                work_ref=work_ref,
                linked_at=now,
            )
        )
        added += 1
    return added


async def stored_session_for(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_session_id: uuid.UUID | None,
    payload: object,
) -> StoredSessionLink | None:
    """Return the stored session of an audit row, or ``None``.

    Call it only for a reader that :func:`may_read_sessions`.
    """
    if agent_session_id is None or payload_session_from_client(payload):
        return None
    linked = select(AgentSessionLink.record_id).where(
        AgentSessionLink.tenant_id == tenant_id,
        AgentSessionLink.mcp_session_id == agent_session_id,
    )
    row = (
        await session.execute(
            select(AgentSessionRecord.id, AgentSessionRecord.session_id, AgentSessionRecord.app)
            .where(
                AgentSessionRecord.tenant_id == tenant_id,
                or_(
                    and_(
                        AgentSessionRecord.id == agent_session_id,
                        AgentSessionRecord.app == HOSTED_RUN_APP,
                    ),
                    AgentSessionRecord.id.in_(linked),
                ),
            )
            .order_by(AgentSessionRecord.last_seen_at.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    # An upload may name any app, so a hosted run also needs the run id.
    hosted_run = row.id == agent_session_id and row.app == HOSTED_RUN_APP
    return StoredSessionLink(session_id=row.session_id, hosted_run=hosted_run)
