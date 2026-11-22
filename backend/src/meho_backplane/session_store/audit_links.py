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

* **A ``meho`` CLI command of an uploaded session.** Its audit rows carry
  the ``meho login`` session id
  (:data:`~meho_backplane.operations._audit.SESSION_FROM_CLIENT_KEY`).
  Many agent sessions share one login, so that id names none of them.
  :func:`link_cli_calls` links each row on its own instead: same tenant,
  same person, a CLI row, its ``op_id`` is a word of the command or its
  command path (``payload["cli_command"]``, sent by the CLI) is a path of
  the command, and it was written while the command ran (30 seconds of
  slack). The path is what links a command such as ``meho vmware vm list``,
  whose ``op_id`` (``GET:/vcenter/vm``) the CLI picks itself. The row id is
  stored as one :class:`AgentSessionAuditLink` row. A row that two stored
  sessions claim (two conversations of one login ran the same operation at
  the same time) links to neither.

All of it is behind the switch ``AUDIT_SESSION_LINKS`` (off by default,
:func:`session_links_on`). Off, an upload stores no link and the read
routes do not look one up.

:func:`stored_session_for` answers the read side. Only a caller that
:func:`may_read_sessions` may see the link; for any other caller the read
routes do not call it, so they give no hint that a session exists.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import MACHINE_PRINCIPAL_KINDS, Operator
from meho_backplane.db.models import AuditLog
from meho_backplane.operations._audit import (
    CLI_COMMAND_KEY,
    SESSION_FROM_CLIENT_KEY,
    agent_session_rows,
    payload_session_from_client,
)
from meho_backplane.operations._validate import compute_audit_params_hash
from meho_backplane.session_store.hosted_runs import HOSTED_RUN_APP
from meho_backplane.session_store.models import (
    AgentSessionAuditLink,
    AgentSessionLink,
    AgentSessionRecord,
)
from meho_backplane.session_store.rights import SessionRight, has_right
from meho_backplane.session_store.tool_calls import CliCall, ToolCall
from meho_backplane.settings import get_settings

__all__ = [
    "StoredSessionLink",
    "link_cli_calls",
    "link_mcp_sessions",
    "may_read_sessions",
    "session_links_on",
    "stored_session_for",
]

#: Clock difference allowed between the laptop's session times and MEHO's
#: audit times.
_TIME_SLACK: Final[timedelta] = timedelta(minutes=5)

#: The ``method`` of the audit row of an MCP tool call.
_MCP_METHOD: Final[str] = "MCP"

#: Clock difference allowed between the time of a ``meho`` command in the
#: transcript and the time of its audit rows. Small, because only the time
#: tells which command wrote a row.
_CLI_SLACK: Final[timedelta] = timedelta(seconds=30)

#: How many command words or paths go into one ``IN`` list of the CLI row query.
_WORDS_PER_QUERY: Final[int] = 500


@dataclass(frozen=True)
class StoredSessionLink:
    """The stored session an audit row belongs to."""

    session_id: str
    """The id to read the session with (``meho sessions show <id>``)."""
    hosted_run: bool
    """True when the session is a hosted agent run; its id is the run id."""


def session_links_on() -> bool:
    """Whether the switch ``AUDIT_SESSION_LINKS`` is on (off by default)."""
    return get_settings().audit_session_links


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
    # No flush here: the caller's commit writes the changes and turns a
    # conflict into a clean 409. The caller inserted a new record already.
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
    audit_id: uuid.UUID,
    agent_session_id: uuid.UUID | None,
    payload: object,
) -> StoredSessionLink | None:
    """Return the stored session of an audit row, or ``None``.

    Call it only for a reader that :func:`may_read_sessions`.
    """
    if agent_session_id is None:
        return None
    if payload_session_from_client(payload):
        return await _stored_session_of_cli_row(session, tenant_id=tenant_id, audit_id=audit_id)
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


async def _stored_session_of_cli_row(
    session: AsyncSession, *, tenant_id: uuid.UUID, audit_id: uuid.UUID
) -> StoredSessionLink | None:
    """Return the one stored session that claims the CLI row *audit_id*, or ``None``."""
    rows = (
        await session.execute(
            select(AgentSessionRecord.session_id)
            .join(AgentSessionAuditLink, AgentSessionAuditLink.record_id == AgentSessionRecord.id)
            .where(
                AgentSessionAuditLink.tenant_id == tenant_id,
                AgentSessionAuditLink.audit_id == audit_id,
                AgentSessionRecord.tenant_id == tenant_id,
            )
            .limit(2)
        )
    ).all()
    # Two sessions claim the row: it says nothing about which one ran it.
    if len(rows) != 1:
        return None
    return StoredSessionLink(session_id=rows[0].session_id, hosted_run=False)


def _cli_row_matches(
    op_id: object, command: object, occurred_at: datetime, calls: Sequence[CliCall]
) -> bool:
    if occurred_at.tzinfo is None:
        # SQLite gives the stored UTC time without its time zone.
        occurred_at = occurred_at.replace(tzinfo=UTC)
    return any(
        (op_id in call.words or command in call.paths)
        and call.started_at - _CLI_SLACK <= occurred_at <= call.ended_at + _CLI_SLACK
        for call in calls
    )


async def _matching_cli_rows(
    session: AsyncSession, record: AgentSessionRecord, calls: Sequence[CliCall]
) -> set[uuid.UUID]:
    op_id = AuditLog.payload["op_id"].as_string()
    command = AuditLog.payload[CLI_COMMAND_KEY].as_string()
    # A row matches by its op_id (``meho call k8s pods.list``) or by the
    # command path the CLI sent (``meho vmware vm list``).
    lookups = [
        (op_id, sorted(set().union(*(call.words for call in calls)))),
        (command, sorted(set().union(*(call.paths for call in calls)))),
    ]
    begin = min(call.started_at for call in calls) - _CLI_SLACK
    end = max(call.ended_at for call in calls) + _CLI_SLACK
    matched: set[uuid.UUID] = set()
    for column, values in lookups:
        for start in range(0, len(values), _WORDS_PER_QUERY):
            result = await session.execute(
                select(AuditLog.id, op_id, command, AuditLog.occurred_at).where(
                    AuditLog.tenant_id == record.tenant_id,
                    AuditLog.operator_sub == record.uploaded_by_sub,
                    AuditLog.agent_session_id.is_not(None),
                    AuditLog.payload[SESSION_FROM_CLIENT_KEY].as_boolean().is_(True),
                    column.in_(values[start : start + _WORDS_PER_QUERY]),
                    AuditLog.occurred_at >= begin,
                    AuditLog.occurred_at <= end,
                )
            )
            matched.update(
                row_id
                for row_id, row_op, row_command, when in result.all()
                if _cli_row_matches(row_op, row_command, when, calls)
            )
    return matched


async def link_cli_calls(
    session: AsyncSession,
    record: AgentSessionRecord,
    cli_calls: Sequence[CliCall],
    *,
    now: datetime,
) -> int:
    """Store the audit rows of the ``meho`` commands *cli_calls* for *record*; the caller commits.

    Returns how many new audit rows were linked.
    """
    if not cli_calls:
        return 0
    with session.no_autoflush:
        matched = await _matching_cli_rows(session, record, cli_calls)
        if not matched:
            return 0
        existing = set(
            await session.scalars(
                select(AgentSessionAuditLink.audit_id).where(
                    AgentSessionAuditLink.record_id == record.id
                )
            )
        )
    new = sorted(matched - existing)
    session.add_all(
        AgentSessionAuditLink(
            record_id=record.id, audit_id=audit_id, tenant_id=record.tenant_id, linked_at=now
        )
        for audit_id in new
    )
    return len(new)
