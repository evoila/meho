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
* ``POST /api/v1/agents/runs/{handle}/cancel`` — cancel a non-terminal
  run. Transitions a ``pending`` / ``running`` / ``awaiting_approval`` run
  to ``cancelled`` via the shared ``cancel_run`` service path and returns
  the updated :class:`AgentRunSummaryResponse`. 404 for an unknown /
  cross-tenant handle; 409 for an already-terminal run. Role:
  ``operator``.
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

import contextlib
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import datetime
from typing import Annotated, Final

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query
from fastapi import status as http_status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.agent.invocation import (
    AgentDisabledError,
    AgentNotFoundError,
    AgentRunNotFoundError,
    AgentRunOutcome,
    AgentRunSummary,
    BudgetExceededError,
    get_agent_invoker,
)
from meho_backplane.agent.run import AgentRunEvent
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.db.models import AgentRunStatus
from meho_backplane.operations._audit import work_ref_var
from meho_backplane.operations.agent_run import (
    IllegalTransitionError,
    UnauthorizedCancellationError,
)

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
    "cancel": "agent.cancel_run",
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
    agent_definition_id: uuid.UUID | None
    agent_name: str | None


class AgentRunSummaryResponse(BaseModel):
    """One row of the agent-run list (``GET /agents/runs``).

    A scannable index row: identity, lifecycle state, resolved model
    coordinates, timestamps, and the ``work_ref`` change-ticket
    reference the list filters on (work_ref I3-T2 #1662). The full
    ``output`` blob is omitted — a caller wanting a run's result polls
    ``GET /agents/runs/{handle}``.
    """

    model_config = ConfigDict(frozen=True)

    run_id: uuid.UUID
    status: AgentRunStatus
    trigger: str
    model_tier: str
    provider: str | None
    model: str | None
    turns: int
    work_ref: str | None
    agent_definition_id: uuid.UUID | None
    agent_name: str | None
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None


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


@contextlib.contextmanager
def _bound_work_ref(raw: str | None) -> Iterator[None]:
    """Bind the inbound ``Meho-Work-Ref`` header onto ``work_ref_var``.

    work_ref I3-T2 (#1662): the agent runner reads ``work_ref_var`` when it
    creates the durable run row, but the chassis audit middleware only binds
    the header around its *post-response* audit write (so the binding is not
    live during the route handler). This binds the same header value for the
    duration of the invoker call so the run row inherits it, mirroring the
    set/reset discipline the approval-queue boundary uses. A missing /
    blank header binds nothing — the run's ``work_ref`` lands ``NULL``.
    """
    cleaned = raw.strip() if raw is not None else None
    if not cleaned:
        yield
        return
    token = work_ref_var.set(cleaned)
    try:
        yield
    finally:
        work_ref_var.reset(token)


def _summary_response(summary: AgentRunSummary) -> AgentRunSummaryResponse:
    """Project an :class:`AgentRunSummary` onto the list-row wire model."""
    return AgentRunSummaryResponse(
        run_id=summary.run_id,
        status=summary.status,
        trigger=summary.trigger,
        model_tier=summary.model_tier,
        provider=summary.provider,
        model=summary.model,
        turns=summary.turns,
        work_ref=summary.work_ref,
        agent_definition_id=summary.agent_definition_id,
        agent_name=summary.agent_name,
        created_at=summary.created_at,
        started_at=summary.started_at,
        ended_at=summary.ended_at,
    )


@router.post("/{name}/run")
async def run_agent(
    name: Annotated[str, Path(max_length=_NAME_MAX_LENGTH)],
    body: AgentRunRequest,
    operator: Operator = _require_operator,
    meho_work_ref: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """Run the named agent for the operator's tenant.

    Sync (default) blocks up to ``agent_sync_timeout_seconds`` and returns
    the final output at 200; a longer run converts to async and returns a
    handle at 202. ``async=true`` returns the handle immediately. A
    cross-tenant / absent name is 404; a disabled definition is 409; a
    budget-refused run (per-identity cap reached, per-tenant or global
    kill switch on — G11.5-T6 #1080) is 429 with the reason in the
    ``detail`` body so the operator sees which gate fired.

    The optional ``Meho-Work-Ref`` header binds the run to an external
    change ticket (work_ref I3-T2 #1662); it is stamped on the durable run
    row at create time and filterable on ``GET /agents/runs``.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_RUN_OP_IDS["run"],
        audit_op_class="write",
        audit_agent_name=name,
    )
    invoker = get_agent_invoker()
    try:
        with _bound_work_ref(meho_work_ref):
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
    except BudgetExceededError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "budget_exceeded", "reason": exc.reason},
        ) from exc
    return _outcome_response(outcome)


@router.get("/runs", response_model=list[AgentRunSummaryResponse])
async def list_runs(
    work_ref: str | None = Query(
        default=None,
        description=(
            "Filter by external change-ticket reference (exact match), e.g. "
            "'gh:evoila/meho#11' — the runs that worked under change ticket X "
            "(work_ref I3-T2 #1662). Omit for no work_ref filter."
        ),
    ),
    status: AgentRunStatus | None = Query(
        default=None,
        description=(
            "Filter by lifecycle status (pending / running / awaiting_approval / "
            "succeeded / failed / cancelled). Omit for every state."
        ),
    ),
    agent_name: str | None = Query(
        default=None,
        description=(
            "Filter by agent definition name (exact match) — the runs "
            "produced by agent X (#2472). An unknown name returns an empty "
            "list, not an error. Omit for no agent filter."
        ),
    ),
    limit: int = Query(
        default=100,
        ge=1,
        le=500,
        description="Max runs per page (1..500, default 100).",
    ),
    offset: int = Query(
        default=0,
        ge=0,
        description="Rows to skip for paging.",
    ),
    operator: Operator = _require_operator,
) -> list[AgentRunSummaryResponse]:
    """List the operator's tenant's agent runs, newest first.

    Tenant-isolated server-side via the JWT — cross-tenant runs are
    invisible. ``?work_ref=gh:evoila/meho#11`` narrows to runs under one
    change ticket (exact match); ``?status=running`` narrows to one
    lifecycle state; ``?agent_name=triage`` narrows to runs produced by
    that agent definition (an unknown name yields an empty list). Returns
    ``created_at DESC``.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id="agent.list_runs",
        audit_op_class="read",
    )
    invoker = get_agent_invoker()
    summaries = await invoker.list_runs(
        operator,
        work_ref=work_ref,
        status=status,
        agent_name=agent_name,
        limit=limit,
        offset=offset,
    )
    return [_summary_response(s) for s in summaries]


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
        agent_definition_id=view.agent_definition_id,
        agent_name=view.agent_name,
    )


@router.post("/runs/{handle}/cancel", response_model=AgentRunSummaryResponse)
async def cancel_run(
    handle: uuid.UUID,
    operator: Operator = _require_operator,
) -> AgentRunSummaryResponse:
    """Cancel a non-terminal run by handle (the run id).

    Transitions a ``pending`` / ``running`` / ``awaiting_approval`` run to
    ``cancelled`` through the shared
    :func:`~meho_backplane.operations.agent_run.cancel_run` service path --
    the same terminal-transition the reaper writes, so the durable state
    and the ``agent_run.completed`` lifecycle event are produced by one
    code path (no second status-write). The async loop is not torn down
    synchronously: the durable cancel intent is recorded and the loop
    observes it on its next turn boundary.

    An unknown / cross-tenant handle is 404 (existence is not leaked across
    tenants, matching the poll route). An already-terminal run -- including
    one that *raced* to ``succeeded`` / ``failed`` between the operator's
    request and the write -- is 409, not a 500. A ``read_only`` operator is
    rejected by the route's role gate; the service's own role check is a
    defence-in-depth backstop mapped to 403 should it ever fire.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_RUN_OP_IDS["cancel"],
        audit_op_class="write",
    )
    invoker = get_agent_invoker()
    try:
        summary = await invoker.cancel(operator, handle)
    except AgentRunNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="agent_run_not_found",
        ) from exc
    except UnauthorizedCancellationError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="agent_run_cancel_forbidden",
        ) from exc
    except IllegalTransitionError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="agent_run_not_cancellable",
        ) from exc
    return _summary_response(summary)


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
    work_ref: str | None,
) -> AsyncIterator[str]:
    """SSE generator: drive a fresh run inline, yield one frame per event.

    A client disconnect propagates as :class:`asyncio.CancelledError` into
    the pending iteration; the underlying loop's ``async with agent.iter``
    cleanup cancels the run, and the cancellation re-raises so the task tree
    unwinds per asyncio's contract (Sonar S7497).

    The work_ref binding (#1662) is held for the whole stream so the run row
    the stream creates inherits the change ticket; it resets when the
    generator finalises (stream end, client disconnect, cancel).
    """
    invoker = get_agent_invoker()
    with _bound_work_ref(work_ref):
        async for run_id, event in invoker.stream_events(operator, name, inputs):
            yield _format_event(run_id, event)


@router.post("/{name}/run/events")
async def run_agent_events(
    name: Annotated[str, Path(max_length=_NAME_MAX_LENGTH)],
    body: AgentRunRequest,
    operator: Operator = _require_operator,
    meho_work_ref: Annotated[str | None, Header()] = None,
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
    # disabled / budget-refused error surfaces as a clean HTTP status,
    # not a dropped SSE connection the EventSource would auto-reconnect
    # into a hot loop.
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
    except BudgetExceededError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "budget_exceeded", "reason": exc.reason},
        ) from exc
    return StreamingResponse(
        _events_generator(operator, name, body.input, meho_work_ref),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "X-Accel-Buffering": "no",
        },
    )
