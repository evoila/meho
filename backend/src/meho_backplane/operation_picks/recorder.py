# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Write one record per operation search and per operation call (#4750).

The entry points of the agent surface wrap their call in
:func:`recorded_search` or :func:`recorded_call`: the MCP tools, the REST
routes the CLI uses, and the hosted agent's tools. The wrapper runs the
call, then writes the record. The answer is returned unchanged.

* A search record holds the connector, group, limit, outcome, the
  returned operation ids in rank order, and a SHA-256 hash of the query.
  The query text only when ``OPERATION_SEARCH_STORE_QUERY_TEXT`` is on.
* A call record holds the connector, ``op_id`` and outcome, and links to
  the newest search of the same tenant, caller, session and connector
  within :data:`LINK_WINDOW`. ``rank`` is the place of ``op_id`` in that
  search's results.
* A call started in the background (:func:`recorded_background_call`) is
  written as ``pending`` before it starts. When the background run ends,
  the same record gets the outcome of the call; the search link stays.

Never changes an answer: with ``REQUEST_USAGE_RETENTION_DAYS`` unset
nothing is written, and a failed write is logged (exception class only)
and swallowed. An exception of the wrapped call is recorded and raised
again unchanged.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

import structlog
from sqlalchemy import select, update

from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import OperationCallRecord, OperationSearchRecord
from meho_backplane.operations._contract import UnknownOperationError
from meho_backplane.operations.meta_tools import (
    SEARCH_LIMIT_MAX,
    ConnectorNotIngestedError,
    UnknownConnectorError,
)
from meho_backplane.operations.operation_run_service import DispatchWrapper
from meho_backplane.request_usage.collector import (
    collection_enabled,
    current_request_id,
    current_session_id,
)
from meho_backplane.settings import get_settings

__all__ = [
    "LINK_WINDOW",
    "QUERY_TEXT_MAX_CHARS",
    "Transport",
    "recorded_background_call",
    "recorded_call",
    "recorded_meta_tool",
    "recorded_search",
]

_log = structlog.get_logger(__name__)

#: Where the call came from. ``agent`` is a hosted agent run.
Transport = Literal["mcp", "rest", "agent"]

#: How far back a call looks for the search it followed. The same five
#: minutes ``meho retrieval usage`` uses for its conversion count.
LINK_WINDOW: Final[timedelta] = timedelta(minutes=5)

#: Longest query text stored when the query-text setting is on.
QUERY_TEXT_MAX_CHARS: Final[int] = 2000

#: Longest connector id or op id stored. Longer values are cut.
_ID_MAX_CHARS: Final[int] = 512

#: Call statuses stored as they are. Any other status is stored as ``other``.
_CALL_STATUSES: Final[frozenset[str]] = frozenset(
    {"ok", "error", "denied", "pending", "awaiting_approval", "rate_limited"}
)

AnswerRunner = Callable[[], Awaitable[dict[str, Any]]]


def _error_outcome(exc: Exception) -> str:
    """Return the stored outcome for a search that raised *exc*."""
    if isinstance(exc, UnknownOperationError):
        return "unknown_op"
    if isinstance(exc, UnknownConnectorError):
        return "unknown_connector"
    if isinstance(exc, ConnectorNotIngestedError):
        return "connector_not_ingested"
    return "error"


def _text(value: object) -> str | None:
    """Return *value* cut to the id limit, or ``None`` when it is no text."""
    if not isinstance(value, str) or not value:
        return None
    return value[:_ID_MAX_CHARS]


def _limit(value: object) -> int:
    """Return the limit the search used (same clamp as ``search_operations``)."""
    if isinstance(value, bool) or not isinstance(value, int | str):
        return 10
    try:
        return min(max(int(value), 1), SEARCH_LIMIT_MAX)
    except ValueError:
        return 10


def _hit_ids(answer: dict[str, Any]) -> list[str]:
    """Return the op ids of a ranked answer, in rank order."""
    hits = answer.get("hits")
    if not isinstance(hits, list):
        return []
    return [
        hit["op_id"] for hit in hits if isinstance(hit, dict) and isinstance(hit.get("op_id"), str)
    ]


def _search_result(
    answer: dict[str, Any] | None, error: Exception | None, op_id: str | None
) -> tuple[str, list[str]]:
    """Return ``(outcome, returned op ids)`` of one search."""
    if error is not None:
        return _error_outcome(error), []
    if answer is None:
        return "error", []
    if op_id is not None and "operation" in answer:
        return "exact_op", [op_id]
    outcome = answer.get("outcome") or answer.get("status")
    return (outcome if isinstance(outcome, str) else "error"), _hit_ids(answer)


def _search_row(
    operator: Operator,
    arguments: dict[str, Any],
    *,
    transport: Transport,
    occurred_at: datetime,
    answer: dict[str, Any] | None,
    error: Exception | None,
) -> OperationSearchRecord | None:
    """Build the search record, or ``None`` when the call named no connector."""
    connector_id = _text(arguments.get("connector_id"))
    if connector_id is None:
        return None
    op_id = _text(arguments.get("op_id"))
    outcome, op_ids = _search_result(answer, error, op_id)
    raw_query = arguments.get("query")
    query = raw_query if op_id is None and isinstance(raw_query, str) else None
    store_text = query is not None and get_settings().operation_search_store_query_text
    return OperationSearchRecord(
        id=uuid.uuid4(),
        occurred_at=occurred_at,
        tenant_id=operator.tenant_id,
        principal_sub=operator.sub,
        session_id=current_session_id(),
        request_id=current_request_id(),
        transport=transport,
        connector_id=connector_id,
        mode="ranked" if op_id is None else "exact",
        group_key=_text(arguments.get("group")) if op_id is None else None,
        search_limit=_limit(arguments.get("limit", 10)) if op_id is None else None,
        outcome=outcome,
        hit_count=len(op_ids),
        op_ids=op_ids,
        query_hash=(hashlib.sha256(query.encode("utf-8")).hexdigest() if query else None),
        query_text=query[:QUERY_TEXT_MAX_CHARS] if query and store_text else None,
    )


def _call_outcome(answer: dict[str, Any]) -> str:
    """Return the stored outcome of one call: its status, or ``unknown_op``."""
    extras = answer.get("extras")
    if isinstance(extras, dict) and extras.get("error_code") == "unknown_op":
        return "unknown_op"
    status = answer.get("status")
    if isinstance(status, str) and status in _CALL_STATUSES:
        return status
    return "other"


async def _write_search(row: OperationSearchRecord | None) -> None:
    if row is None:
        return
    async with get_sessionmaker()() as session:
        session.add(row)
        await session.commit()


async def _write_call(
    operator: Operator,
    arguments: dict[str, Any],
    *,
    transport: Transport,
    occurred_at: datetime,
    outcome: str,
    record_id: uuid.UUID | None = None,
) -> None:
    """Link the call to its search and write the call record."""
    connector_id = _text(arguments.get("connector_id"))
    op_id = _text(arguments.get("op_id"))
    if connector_id is None or op_id is None:
        return
    session_id = current_session_id()
    same_session = (
        OperationSearchRecord.session_id.is_(None)
        if session_id is None
        else OperationSearchRecord.session_id == session_id
    )
    newest_search = (
        select(OperationSearchRecord.id, OperationSearchRecord.op_ids)
        .where(
            OperationSearchRecord.tenant_id == operator.tenant_id,
            OperationSearchRecord.principal_sub == operator.sub,
            OperationSearchRecord.connector_id == connector_id,
            OperationSearchRecord.occurred_at >= occurred_at - LINK_WINDOW,
            OperationSearchRecord.occurred_at <= occurred_at,
            same_session,
        )
        .order_by(OperationSearchRecord.occurred_at.desc())
        .limit(1)
    )
    async with get_sessionmaker()() as session:
        found = (await session.execute(newest_search)).first()
        search_id: uuid.UUID | None = None
        rank: int | None = None
        if found is not None:
            search_id = found.id
            op_ids = found.op_ids if isinstance(found.op_ids, list) else []
            rank = op_ids.index(op_id) + 1 if op_id in op_ids else None
        session.add(
            OperationCallRecord(
                id=record_id or uuid.uuid4(),
                occurred_at=occurred_at,
                tenant_id=operator.tenant_id,
                principal_sub=operator.sub,
                session_id=session_id,
                request_id=current_request_id(),
                transport=transport,
                connector_id=connector_id,
                op_id=op_id,
                outcome=outcome,
                search_id=search_id,
                rank=rank,
            )
        )
        await session.commit()


async def _set_call_outcome(record_id: uuid.UUID, outcome: str) -> None:
    """Set the outcome of a call record written before its background run."""
    async with get_sessionmaker()() as session:
        await session.execute(
            update(OperationCallRecord)
            .where(OperationCallRecord.id == record_id)
            .values(outcome=outcome)
        )
        await session.commit()


async def _write_safely(kind: str, write: Callable[[], Awaitable[None]]) -> None:
    """Run *write*; log a failure by exception class only and go on."""
    try:
        await write()
    except Exception as exc:
        # Only the class name: driver errors can echo statement parameters.
        _log.warning("operation_pick_record_failed", record=kind, error_type=type(exc).__name__)


async def recorded_search(
    operator: Operator,
    arguments: dict[str, Any],
    *,
    transport: Transport,
    run: AnswerRunner,
) -> dict[str, Any]:
    """Run one ``search_operations`` call and write its search record."""
    if not collection_enabled():
        return await run()
    occurred_at = datetime.now(UTC)

    async def _write(answer: dict[str, Any] | None, error: Exception | None) -> None:
        row = _search_row(
            operator,
            arguments,
            transport=transport,
            occurred_at=occurred_at,
            answer=answer,
            error=error,
        )
        await _write_search(row)

    try:
        answer = await run()
    except Exception as exc:
        error = exc
        await _write_safely("search", lambda: _write(None, error))
        raise
    await _write_safely("search", lambda: _write(answer, None))
    return answer


async def recorded_call(
    operator: Operator,
    arguments: dict[str, Any],
    *,
    transport: Transport,
    run: AnswerRunner,
) -> dict[str, Any]:
    """Run one ``call_operation`` call and write its call record."""
    if not collection_enabled():
        return await run()
    occurred_at = datetime.now(UTC)

    async def _write(outcome: str) -> None:
        await _write_call(
            operator,
            arguments,
            transport=transport,
            occurred_at=occurred_at,
            outcome=outcome,
        )

    try:
        answer = await run()
    except Exception:
        await _write_safely("call", lambda: _write("error"))
        raise
    await _write_safely("call", lambda: _write(_call_outcome(answer)))
    return answer


async def recorded_background_call(
    operator: Operator,
    arguments: dict[str, Any],
    *,
    transport: Transport,
    submit: Callable[[DispatchWrapper | None], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Start one ``call_operation`` call in the background and record it.

    *submit* starts the run and passes the wrapper it gets on to
    :meth:`OperationRunService.submit_call`. The record is written as
    ``pending`` before the run starts, so the run can never end before
    its record exists. When the run ends, the wrapper sets the record's
    outcome from the call's answer (``error`` when the call raised). A
    start that raises sets ``error``. A run cancelled before it starts
    keeps ``pending``.
    """
    if not collection_enabled():
        return await submit(None)
    record_id = uuid.uuid4()
    await _write_safely(
        "call",
        lambda: _write_call(
            operator,
            arguments,
            transport=transport,
            occurred_at=datetime.now(UTC),
            outcome="pending",
            record_id=record_id,
        ),
    )

    async def _finish(outcome: str) -> None:
        await _write_safely("call", lambda: _set_call_outcome(record_id, outcome))

    async def wrap(dispatch: AnswerRunner) -> dict[str, Any]:
        try:
            answer = await dispatch()
        except Exception:
            await _finish("error")
            raise
        await _finish(_call_outcome(answer))
        return answer

    try:
        return await submit(wrap)
    except Exception:
        await _finish("error")
        raise


async def recorded_meta_tool(
    name: str,
    operator: Operator,
    arguments: dict[str, Any],
    *,
    run: AnswerRunner,
) -> dict[str, Any]:
    """Record a hosted agent's ``search_operations`` / ``call_operation`` call.

    Any other tool runs without a record.
    """
    if name == "search_operations":
        return await recorded_search(operator, arguments, transport="agent", run=run)
    if name == "call_operation":
        return await recorded_call(operator, arguments, transport="agent", run=run)
    return await run()
