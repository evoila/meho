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
* ``wait_seconds`` (with ``run_id``) makes the check wait up to that many
  seconds for the run to end (#5264). An agent with no shell cannot sleep
  between checks, so without it the agent would send check after check.

No new MCP tool is added: the working surface does not change.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.models import OperationRun, OperationRunStatus
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.operations.delivery_budget import bound_complete_response
from meho_backplane.operations.operation_run import OperationRunNotFoundError
from meho_backplane.operations.operation_run_service import (
    get_operation_run_service,
    run_result_visible_to,
)
from meho_backplane.settings import get_settings

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


def _stored_result_with_run_details(
    stored: dict[str, Any], run_extras: dict[str, Any]
) -> dict[str, Any]:
    """Add the run details to a finished call's stored result.

    The stored result passed the wire ceiling (``bound_complete_response``)
    when the call finished. The run details make it larger, so the ceiling
    is checked again on the answer that is sent. When it no longer fits,
    the answer is trimmed the same way as a synchronous call: ``status``
    stays ``ok``, the ``audit_id`` stays, and ``delivery`` says what was
    left out.
    """
    stored_extras = stored.get("extras")
    merged = {
        **stored,
        "extras": {**(stored_extras if isinstance(stored_extras, dict) else {}), **run_extras},
    }
    result = OperationResult.model_validate(merged)
    bounded = bound_complete_response(
        result, get_settings().result_complete_response_wire_max_bytes
    )
    return bounded.model_dump(mode="json")


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
    operator: Operator,
    arguments: dict[str, Any],
    raw_run_id: str,
    *,
    wait_seconds: float = 0,
) -> dict[str, Any]:
    """Read an async run started by :func:`submit_async_call`.

    Tenant-scoped like ``GET /api/v1/operations/runs/{handle}``. The run must
    also match the ``connector_id`` and ``op_id`` of this call, and must have
    been started by the caller; an unknown, other-tenant, mismatched or
    other-person run gives the same ``unknown_run`` error, so the answer
    tells nothing about other runs. With ``wait_seconds`` above 0 the check
    waits up to that long for the run to end; the run is checked against the
    call first, so a wrong ``run_id`` is refused at once. The tool's
    ``inputSchema`` keeps ``wait_seconds`` between 0 and the limit.
    """
    try:
        run_id = uuid.UUID(raw_run_id)
    except ValueError as exc:
        raise _unknown_run(raw_run_id) from exc
    service = get_operation_run_service()
    try:
        row = await service.poll(operator, run_id)
    except OperationRunNotFoundError as exc:
        raise _unknown_run(raw_run_id) from exc
    op_id = str(arguments["op_id"])
    if (
        row.connector_id != arguments["connector_id"]
        or row.op_id != op_id
        or not run_result_visible_to(row, operator)
    ):
        raise _unknown_run(raw_run_id)
    if wait_seconds > 0:
        try:
            row = await service.wait_until_done(operator, row, wait_seconds=wait_seconds)
        except OperationRunNotFoundError as exc:
            raise _unknown_run(raw_run_id) from exc

    extras = _run_extras(row)
    if row.status == OperationRunStatus.SUCCEEDED and isinstance(row.result, dict):
        return _stored_result_with_run_details(row.result, extras)

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
