# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Async mode for the MCP ``call_operation`` tool (#3695).

The MCP twin of ``POST /api/v1/operations/call`` with ``async: true``
(#3079). A long multi-step call (for example an OVF deploy that runs for
many minutes) should not hold one MCP request open for its whole run:

* ``call_operation`` with ``async: true`` starts the call in the background
  and answers at once with ``status="pending"`` and ``extras.run_id``. It
  uses the same ``operation_run`` row and the same background runner as the
  REST route, so the policy gate, approvals and audit rows are the same.
* ``call_operation`` with the same ``connector_id`` and ``op_id`` plus
  ``run_id`` reads that run. While it runs the answer is ``status="pending"``
  or ``status="running"`` with ``extras.started_at``. When it is done the
  answer is the stored ``OperationResult`` of the call.

No new MCP tool is added: the working surface stays the same 25 tools.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import Any

from meho_backplane.auth.operator import Operator
from meho_backplane.db.models import OperationRun, OperationRunStatus
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.operations.operation_run import OperationRunNotFoundError
from meho_backplane.operations.operation_run_service import get_operation_run_service

__all__ = ["poll_call", "submit_async_call"]

_NEXT_STEP = (
    "The operation runs in the background. To check it, call `call_operation` "
    "again with the same `connector_id` and `op_id` and this `run_id`. Do not "
    "start the same call again."
)


def _aware(value: datetime | None) -> datetime | None:
    """Return *value* as an aware UTC datetime (SQLite returns naive values)."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _iso(value: datetime | None) -> str | None:
    aware = _aware(value)
    return None if aware is None else aware.isoformat()


def _run_extras(row: OperationRun) -> dict[str, Any]:
    return {
        "run_id": str(row.id),
        "run_status": row.status,
        "created_at": _iso(row.created_at),
        "started_at": _iso(row.started_at),
        "ended_at": _iso(row.ended_at),
    }


def _envelope(
    op_id: str,
    *,
    status: str,
    duration_ms: float,
    extras: dict[str, Any],
    error: str | None = None,
) -> dict[str, Any]:
    """Build an ``OperationResult``-shaped answer that matches the outputSchema."""
    return {
        "status": status,
        "op_id": op_id,
        "result": None,
        "error": error,
        "duration_ms": round(duration_ms, 2),
        "audit_id": None,
        "delivery": None,
        "handle": None,
        "extras": extras,
    }


async def submit_async_call(operator: Operator, arguments: dict[str, Any]) -> dict[str, Any]:
    """Start the call in the background and return a ``pending`` answer."""
    started = time.monotonic()
    run_id = await get_operation_run_service().submit_call(operator, arguments)
    return _envelope(
        str(arguments["op_id"]),
        status="pending",
        duration_ms=(time.monotonic() - started) * 1000,
        extras={"run_id": str(run_id), "async": True, "next_step": _NEXT_STEP},
    )


def _unknown_run(raw: str) -> McpInvalidParamsError:
    return McpInvalidParamsError(
        f"no async run {raw!r} for this connector_id and op_id",
        data={"reason": "unknown_run", "run_id": raw},
    )


async def poll_call(
    operator: Operator, arguments: dict[str, Any], raw_run_id: str
) -> dict[str, Any]:
    """Read an async run started by :func:`submit_async_call`.

    Tenant-scoped like ``GET /api/v1/operations/runs/{handle}``. The run must
    also match the ``connector_id`` and ``op_id`` of this call; an unknown,
    other-tenant or mismatched run gives the same ``unknown_run`` error, so
    nothing about other runs leaks.
    """
    try:
        run_id = uuid.UUID(raw_run_id)
    except ValueError as exc:
        raise _unknown_run(raw_run_id) from exc
    try:
        row = await get_operation_run_service().poll(operator, run_id)
    except OperationRunNotFoundError as exc:
        raise _unknown_run(raw_run_id) from exc
    op_id = str(arguments["op_id"])
    if row.connector_id != arguments["connector_id"] or row.op_id != op_id:
        raise _unknown_run(raw_run_id)

    extras = _run_extras(row)
    if row.status == OperationRunStatus.SUCCEEDED and isinstance(row.result, dict):
        result: dict[str, Any] = dict(row.result)
        stored_extras = result.get("extras")
        result["extras"] = {**(stored_extras if isinstance(stored_extras, dict) else {}), **extras}
        return result

    since = _aware(row.started_at) or _aware(row.created_at)
    until = _aware(row.ended_at) or datetime.now(UTC)
    elapsed_ms = max((until - since).total_seconds() * 1000, 0.0) if since else 0.0
    if row.status in (OperationRunStatus.PENDING, OperationRunStatus.RUNNING):
        extras["next_step"] = _NEXT_STEP
        return _envelope(op_id, status=row.status, duration_ms=elapsed_ms, extras=extras)

    # failed / cancelled: the run gave no result. The call may have done part
    # of its work, so the caller must check the audit log before a retry.
    extras["error_code"] = f"run_{row.status}"
    reason = row.error or f"the run was {row.status}"
    return _envelope(
        op_id,
        status="error",
        duration_ms=elapsed_ms,
        extras=extras,
        error=f"run_{row.status}: {reason}. Check the audit log before you start it again.",
    )
