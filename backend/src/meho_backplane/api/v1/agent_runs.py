# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/agents/{name}/run`` + ``/runs/*`` — agent invocation surface.

G11.1-T4 (#811) under Initiative #802 (the P1 agent runtime). The public
way to *run* a defined agent over REST: synchronous block-and-return for
short runs, asynchronous handle + poll + SSE for long ones. The MCP verbs
(:mod:`meho_backplane.mcp.tools.agent_runs`) and the Go CLI verbs
(``cli/internal/cmd/agent``) drive the same
:class:`~meho_backplane.agent.invocation.AgentInvoker` from their own
transports; this module is the HTTP front.

Route inventory
---------------

* ``POST /api/v1/agents/{name}/run`` — run the named agent. Body:
  :class:`AgentRunRequest` (``input`` text + optional ``async_`` flag).
  Sync (default): blocks up to the server-side timeout and returns the
  final output (:class:`AgentRunResultResponse`, HTTP 200); a run that
  exceeds the timeout, or an ``async_=true`` request, returns the run
  handle (:class:`AgentRunHandleResponse`, HTTP 202). Role: ``operator``.
* ``GET /api/v1/agents/runs/{handle}`` — poll a run's durable status.
  Returns :class:`AgentRunStatusResponse`; 404 for an unknown /
  cross-tenant handle. Role: ``operator``.
* ``GET /api/v1/agents/runs/{handle}/events`` — Server-Sent Events stream
  of a *fresh* run's events. **Note:** the handle path segment names the
  agent definition, not an existing run — an SSE stream drives a new run
  inline (one connection = one run's lifetime), the WHATWG ``EventSource``
  shape the G6 broadcast feed established. Role: ``operator``.

The poll and events routes sit under a ``/runs`` sub-path that is two
segments deep, so they never collide with the one-segment ``/{name}``
definition-CRUD routes on the same prefix.

Tenant scoping + RBAC
---------------------

Every route derives ``tenant_id`` from the JWT-validated
:class:`~meho_backplane.auth.operator.Operator` and requires the
``operator`` role (``read_only`` → 403). A run executes only an *enabled*
definition in the operator's tenant; a cross-tenant / absent name is a
404, a disabled definition a 409 — the
:class:`~meho_backplane.agent.invocation.AgentInvoker` raises the typed
errors this module maps.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi import status as http_status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.agent.invocation import (
    AgentDisabledError,
    AgentNotFoundError,
    AgentRunNotFoundError,
    AgentRunOutcome,
    get_agent_invoker,
)
from meho_backplane.agent.run import AgentRunEvent
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.db.models import AgentRunStatus

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])

_log = structlog.get_logger(__name__)

#: Operator-minimum gate, module-scoped to satisfy ruff B008 (no call in a
#: default-argument position). Mirrors :mod:`meho_backplane.api.v1.agents`.
_require_operator = Depends(require_role(TenantRole.OPERATOR))

#: Canonical op ids bound into ``audit_op_id`` per route. ``run`` /
#: ``run_events`` are writes (they execute a loop that may call write ops);
#: ``run_status`` is a read.
_RUN_OP_IDS: Final[dict[str, str]] = {
    "run": "agent.run",
    "status": "agent.run_status",
    "events": "agent.run_events",
}

#: Max length of the ``{name}`` path parameter — same defence-in-depth cap
#: the definition-CRUD routes apply.
_NAME_MAX_LENGTH: Final[int] = 128


class AgentRunRequest(BaseModel):
    """POST body for ``/agents/{name}/run``.

    ``extra="forbid"`` rejects unknown fields with 422 (catches a client
    typo before it lands as a silent no-op). ``async_`` is the async flag —
    named with a trailing underscore because ``async`` is a Python keyword;
    its JSON alias is ``async`` so the wire field reads naturally.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    input: str = Field(min_length=1, description="The user prompt to run the agent on.")
    async_: bool = Field(
        default=False,
        alias="async",
        description="Return a run handle immediately instead of blocking for the result.",
    )


class AgentRunResultResponse(BaseModel):
    """Terminal response for a completed synchronous run (HTTP 200)."""

    model_config = ConfigDict(frozen=True)

    run_id: uuid.UUID
    status: AgentRunStatus
    output: dict[str, object] | None
    error: str | None


class AgentRunHandleResponse(BaseModel):
    """Handle response for an async run or a sync run that timed out (HTTP 202)."""

    model_config = ConfigDict(frozen=True)

    run_id: uuid.UUID
    status: AgentRunStatus
    converted_to_async: bool


class AgentRunStatusResponse(BaseModel):
    """Poll response for ``GET /agents/runs/{handle}``."""

    model_config = ConfigDict(frozen=True)

    run_id: uuid.UUID
    status: AgentRunStatus
    turns: int
    provider: str | None
    model: str | None
    output: dict[str, object] | None
    error: str | None


def _outcome_response(outcome: AgentRunOutcome) -> JSONResponse:
    """Render an :class:`AgentRunOutcome` as the right JSON response.

    A terminal sync outcome returns the result at HTTP 200; an async (or
    converted-to-async) outcome returns the handle at HTTP 202 so the
    operator's client knows to poll rather than expecting a result body.
    """
    if outcome.status in {
        AgentRunStatus.SUCCEEDED,
        AgentRunStatus.FAILED,
        AgentRunStatus.CANCELLED,
    }:
        body = AgentRunResultResponse(
            run_id=outcome.run_id,
            status=outcome.status,
            output=outcome.output,
            error=outcome.error,
        )
        return JSONResponse(
            content=body.model_dump(mode="json"),
            status_code=http_status.HTTP_200_OK,
        )
    handle = AgentRunHandleResponse(
        run_id=outcome.run_id,
        status=outcome.status,
        converted_to_async=outcome.converted_to_async,
    )
    return JSONResponse(
        content=handle.model_dump(mode="json"),
        status_code=http_status.HTTP_202_ACCEPTED,
    )


@router.post("/{name}/run")
async def run_agent(
    name: Annotated[str, Path(max_length=_NAME_MAX_LENGTH)],
    body: AgentRunRequest,
    operator: Operator = _require_operator,
) -> JSONResponse:
    """Run the named agent for the operator's tenant.

    Sync (default) blocks up to ``agent_sync_timeout_seconds`` and returns
    the final output at 200; a longer run converts to async and returns a
    handle at 202. ``async=true`` returns the handle immediately. A
    cross-tenant / absent name is 404; a disabled definition is 409.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_RUN_OP_IDS["run"],
        audit_op_class="write",
        audit_agent_name=name,
    )
    invoker = get_agent_invoker()
    try:
        outcome = await invoker.run(operator, name, body.input, async_mode=body.async_)
    except AgentNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="agent_not_found",
        ) from exc
    except AgentDisabledError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="agent_disabled",
        ) from exc
    return _outcome_response(outcome)


@router.get("/runs/{handle}", response_model=AgentRunStatusResponse)
async def get_run_status(
    handle: uuid.UUID,
    operator: Operator = _require_operator,
) -> AgentRunStatusResponse:
    """Poll a run's durable status by handle (the run id).

    Reads the durable ``agent_run`` row, so it works after the request that
    started the run has returned. An unknown / cross-tenant handle is 404.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_RUN_OP_IDS["status"],
        audit_op_class="read",
    )
    invoker = get_agent_invoker()
    try:
        view = await invoker.poll(operator, handle)
    except AgentRunNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="agent_run_not_found",
        ) from exc
    return AgentRunStatusResponse(
        run_id=view.run_id,
        status=view.status,
        turns=view.turns,
        provider=view.provider,
        model=view.model,
        output=view.output,
        error=view.error,
    )


def _format_event(run_id: uuid.UUID, event: AgentRunEvent) -> str:
    """Format one :class:`AgentRunEvent` as an SSE frame.

    The ``event:`` name is the event kind (``turn`` / ``tool_call`` /
    ``tool_result`` / ``final`` / ``error``); ``data:`` is the JSON payload
    plus the run id so a consumer can correlate. Single-line JSON keeps the
    SSE ``data:`` field newline-free (the WHATWG spec forbids embedded
    newlines unless split into multiple ``data:`` lines).
    """
    payload = json.dumps({"run_id": str(run_id), **event.data}, separators=(",", ":"))
    return f"event: {event.kind.value}\ndata: {payload}\n\n"


async def _events_generator(
    operator: Operator,
    name: str,
    inputs: str,
) -> AsyncIterator[str]:
    """SSE generator: drive a fresh run inline, yield one frame per event.

    A client disconnect propagates as :class:`asyncio.CancelledError` into
    the pending iteration; the underlying loop's ``async with agent.iter``
    cleanup cancels the run, and the cancellation re-raises so the task tree
    unwinds per asyncio's contract (Sonar S7497).
    """
    invoker = get_agent_invoker()
    async for run_id, event in invoker.stream_events(operator, name, inputs):
        yield _format_event(run_id, event)


@router.post("/{name}/run/events")
async def run_agent_events(
    name: Annotated[str, Path(max_length=_NAME_MAX_LENGTH)],
    body: AgentRunRequest,
    operator: Operator = _require_operator,
) -> StreamingResponse:
    """Stream a fresh run's events for the named agent as Server-Sent Events.

    Drives a new run inline and emits ``turn`` / ``tool_call`` /
    ``tool_result`` / ``final`` / ``error`` events as they happen. The run
    is recorded on a durable ``agent_run`` row, so a consumer can poll the
    run's recorded outcome after the stream ends (the ``data:`` field of
    every frame carries the ``run_id``). A cross-tenant / absent name is
    404; a disabled definition is 409 — resolved before the stream opens so
    the error is a normal HTTP status, not a torn stream.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_RUN_OP_IDS["events"],
        audit_op_class="write",
        audit_agent_name=name,
    )
    invoker = get_agent_invoker()
    # Resolve the definition before opening the stream so a not-found /
    # disabled error surfaces as a clean HTTP status, not a dropped SSE
    # connection the EventSource would auto-reconnect into a hot loop.
    try:
        await invoker.ensure_runnable(operator, name)
    except AgentNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="agent_not_found",
        ) from exc
    except AgentDisabledError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="agent_disabled",
        ) from exc
    return StreamingResponse(
        _events_generator(operator, name, body.input),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "X-Accel-Buffering": "no",
        },
    )
