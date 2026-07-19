# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""MCP tools for the agent invocation surface (G11.1-T4 / #811).

Two ``meho.agents.*`` tools that mirror the REST invocation routes
(:mod:`meho_backplane.api.v1.agent_runs`) onto the MCP transport:

* ``meho.agents.run`` — run a named agent. Sync (default) blocks up to the
  server-side timeout and returns the final output; ``async=true`` (or a
  sync run that exceeds the timeout) returns a run handle. Role:
  ``operator``.
* ``meho.agents.run_status`` — poll a run's durable status by handle. Role:
  ``operator``.

SSE streaming is REST-only: the MCP request/response shape has no
streaming-events transport here, so an MCP caller that wants progress polls
``meho.agents.run_status`` after an async ``meho.agents.run``. Both tools
drive the same :class:`~meho_backplane.agent.invocation.AgentInvoker`
singleton the REST routes use, so a run started over MCP is poll-able over
REST and vice versa — the durable ``agent_run`` row is the shared state.

Error mapping
-------------

The invoker's typed errors map onto the MCP wire shape:
:class:`~meho_backplane.agent.invocation.AgentNotFoundError`,
:class:`~meho_backplane.agent.invocation.AgentDisabledError`, and
:class:`~meho_backplane.agent.invocation.AgentRunNotFoundError` all surface
as :class:`~meho_backplane.mcp.server.McpInvalidParamsError` (JSON-RPC
``-32602``) — the closest spec-blessed shape for "not found" / "conflict",
matching :mod:`meho_backplane.mcp.tools.agents`.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

import structlog

from meho_backplane.agent.invocation import (
    AgentDisabledError,
    AgentNotFoundError,
    AgentRunNotFoundError,
    BudgetExceededError,
    get_agent_invoker,
)
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.models import AgentRunStatus
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.operations._audit import work_ref_var

_log = structlog.get_logger(__name__)

#: Canonical op ids bound into ``audit_op_id`` per tool — the same ids the
#: REST routes use, so a row's op_id is transport-independent.
_RUN_OP_IDS: Final[dict[str, str]] = {
    "run": "agent.run",
    "status": "agent.run_status",
    "list": "agent.list_runs",
}


def _require_name(arguments: dict[str, Any]) -> str:
    """Extract a required non-empty string ``name`` or raise invalid-params."""
    name = arguments.get("name")
    if not isinstance(name, str) or not name:
        raise McpInvalidParamsError("name is required and must be a non-empty string")
    return name


def _require_input(arguments: dict[str, Any]) -> str:
    """Extract a required non-empty string ``input`` or raise invalid-params."""
    value = arguments.get("input")
    if not isinstance(value, str) or not value:
        raise McpInvalidParamsError("input is required and must be a non-empty string")
    return value


# ---------------------------------------------------------------------------
# meho.agents.run
# ---------------------------------------------------------------------------


async def _run_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    name = _require_name(arguments)
    user_input = _require_input(arguments)
    async_mode = bool(arguments.get("async", False))
    work_ref = arguments.get("work_ref")
    if work_ref is not None and not isinstance(work_ref, str):
        raise McpInvalidParamsError("work_ref must be a string when supplied")
    structlog.contextvars.bind_contextvars(
        audit_op_id=_RUN_OP_IDS["run"],
        audit_op_class="write",
        audit_agent_name=name,
    )
    invoker = get_agent_invoker()
    # work_ref I3-T2 (#1662): bind the change-ticket ref onto work_ref_var
    # for the run-create boundary so the durable run row inherits it.
    cleaned_work_ref = work_ref.strip() if isinstance(work_ref, str) else None
    work_ref_token = work_ref_var.set(cleaned_work_ref) if cleaned_work_ref else None
    try:
        outcome = await invoker.run(operator, name, user_input, async_mode=async_mode)
    except AgentNotFoundError as exc:
        raise McpInvalidParamsError("agent_not_found") from exc
    except AgentDisabledError as exc:
        raise McpInvalidParamsError("agent_disabled") from exc
    except BudgetExceededError as exc:
        # G11.5-T6 #1080 — the pre-execution budget gate refused this
        # run. JSON-RPC has no spec-blessed "too many requests" code,
        # so the closest signal is invalid-params with the reason in
        # the message — same shape AgentNotFoundError / AgentDisabledError
        # use here. The reason string carries which gate fired (cap /
        # kill switch / per-identity zero limit).
        raise McpInvalidParamsError(f"budget_exceeded: {exc.reason}") from exc
    finally:
        if work_ref_token is not None:
            work_ref_var.reset(work_ref_token)
    return {
        "run_id": str(outcome.run_id),
        "status": outcome.status.value,
        "output": outcome.output,
        "error": outcome.error,
        "converted_to_async": outcome.converted_to_async,
    }


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.agents.run",
        description=(
            "Run a named agent for the operator's tenant (Initiative #802). "
            "Operator-level. Sync (default) blocks up to the server-side "
            "timeout and returns {run_id, status, output, error}; set "
            "async=true (or let a long sync run convert) to get a handle "
            "back immediately ({run_id, status='running', "
            "converted_to_async}). Poll progress with meho.agents.run_status. "
            "A missing / cross-tenant name returns 'agent_not_found'; a "
            "disabled definition returns 'agent_disabled'."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "description": "Agent definition name to run.",
                },
                "input": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The user prompt to run the agent on.",
                },
                "async": {
                    "type": "boolean",
                    "description": "Return a run handle immediately instead of blocking.",
                },
                "work_ref": {
                    "type": "string",
                    "description": (
                        "Optional external change-ticket reference to bind the run "
                        "to (work_ref I3-T2 #1662), e.g. 'gh:evoila/meho#11'. "
                        "Stamped on the run row and filterable via "
                        "meho.agents.list_runs."
                    ),
                },
            },
            "required": ["name", "input"],
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
        op_class="write",
    ),
    handler=_run_handler,
)


# ---------------------------------------------------------------------------
# meho.agents.run_status
# ---------------------------------------------------------------------------


async def _run_status_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    raw = arguments.get("handle")
    if not isinstance(raw, str) or not raw:
        raise McpInvalidParamsError("handle is required and must be a non-empty string")
    try:
        run_id = uuid.UUID(raw)
    except ValueError as exc:
        raise McpInvalidParamsError("handle must be a valid run id (UUID)") from exc
    structlog.contextvars.bind_contextvars(
        audit_op_id=_RUN_OP_IDS["status"],
        audit_op_class="read",
    )
    invoker = get_agent_invoker()
    try:
        view = await invoker.poll(operator, run_id)
    except AgentRunNotFoundError as exc:
        raise McpInvalidParamsError("agent_run_not_found") from exc
    return {
        "run_id": str(view.run_id),
        "status": view.status.value,
        "turns": view.turns,
        "provider": view.provider,
        "model": view.model,
        "output": view.output,
        "error": view.error,
        "agent_definition_id": (
            str(view.agent_definition_id) if view.agent_definition_id is not None else None
        ),
        "agent_name": view.agent_name,
    }


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.agents.run_status",
        description=(
            "Poll an agent run's durable status by handle (Initiative "
            "#802). Operator-level. Returns {run_id, status, turns, "
            "provider, model, output, error, agent_definition_id, "
            "agent_name}; output/error are set once the run reaches a "
            "terminal state; agent_definition_id / agent_name are null for "
            "an ad-hoc run or a definition deleted after the run (#2472). "
            "Reads the durable run record, so it works after the call that "
            "started the run returned. An unknown / cross-tenant handle "
            "returns 'agent_run_not_found'."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "handle": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The run handle (run id, a UUID) to poll.",
                },
            },
            "required": ["handle"],
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
        op_class="read",
    ),
    handler=_run_status_handler,
)


# ---------------------------------------------------------------------------
# meho.agents.list_runs
# ---------------------------------------------------------------------------


async def _list_runs_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    work_ref = arguments.get("work_ref")
    if work_ref is not None and not isinstance(work_ref, str):
        raise McpInvalidParamsError("work_ref must be a string when supplied")
    agent_name = arguments.get("agent_name")
    if agent_name is not None and not isinstance(agent_name, str):
        raise McpInvalidParamsError("agent_name must be a string when supplied")
    status_arg = arguments.get("status")
    status: AgentRunStatus | None = None
    if status_arg is not None:
        if not isinstance(status_arg, str):
            raise McpInvalidParamsError("status must be a string when supplied")
        try:
            status = AgentRunStatus(status_arg)
        except ValueError as exc:
            raise McpInvalidParamsError(f"unknown status {status_arg!r}") from exc
    limit = arguments.get("limit", 100)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
        raise McpInvalidParamsError("limit must be an integer in 1..500")
    offset = arguments.get("offset", 0)
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise McpInvalidParamsError("offset must be a non-negative integer")
    structlog.contextvars.bind_contextvars(
        audit_op_id=_RUN_OP_IDS["list"],
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
    return {
        "runs": [
            {
                "run_id": str(s.run_id),
                "status": s.status.value,
                "trigger": s.trigger,
                "model_tier": s.model_tier,
                "provider": s.provider,
                "model": s.model,
                "turns": s.turns,
                "work_ref": s.work_ref,
                "agent_definition_id": (
                    str(s.agent_definition_id) if s.agent_definition_id is not None else None
                ),
                "agent_name": s.agent_name,
                "created_at": s.created_at.isoformat(),
                "started_at": s.started_at.isoformat() if s.started_at is not None else None,
                "ended_at": s.ended_at.isoformat() if s.ended_at is not None else None,
            }
            for s in summaries
        ],
    }


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.agents.list_runs",
        description=(
            "List the operator's tenant's agent runs, newest first "
            "(Initiative #802; work_ref I3-T2 #1662). Operator-level. "
            "Returns {runs: [{run_id, status, trigger, model_tier, "
            "provider, model, turns, work_ref, agent_definition_id, "
            "agent_name, created_at, started_at, ended_at}]}; "
            "agent_definition_id / agent_name are null for an ad-hoc run "
            "or a definition deleted after the run (#2472). Filter by "
            "work_ref (exact-match external change-ticket reference, e.g. "
            "'gh:evoila/meho#11'), status, and/or agent_name (exact-match "
            "agent definition name — an unknown name returns an empty "
            "list, not an error); page with limit (1..500, default 100) + "
            "offset. Tenant-isolated server-side — only your tenant's runs "
            "are visible."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "work_ref": {
                    "type": "string",
                    "description": (
                        "Exact-match external change-ticket reference filter, "
                        "e.g. 'gh:evoila/meho#11'."
                    ),
                },
                "agent_name": {
                    "type": "string",
                    "description": (
                        "Exact-match agent definition name filter. An unknown "
                        "name returns an empty list rather than an error."
                    ),
                },
                "status": {
                    "type": "string",
                    "enum": [s.value for s in AgentRunStatus],
                    "description": "Filter by lifecycle status.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "description": "Max runs per page (default 100).",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Rows to skip for paging.",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
        op_class="read",
    ),
    handler=_list_runs_handler,
)
