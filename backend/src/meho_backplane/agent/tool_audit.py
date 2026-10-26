# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""One audit row for every tool call a hosted agent makes (#4234).

Before this module, only ``call_operation`` left a record: the dispatcher
writes its own row. Discovery calls (``search_operations``,
``list_operation_groups``), broadcast reads, and calls that failed before
the dispatcher ran left nothing. A run that failed could leave no row at
all, so MEHO could not show what the agent tried.

:class:`ToolCallAudit` is a Pydantic AI capability. The run loop passes it
to every agent it builds (:meth:`~meho_backplane.agent.run.PydanticAgentRun._build_agent`),
so it wraps every function tool: the meta-tools, the legacy two-tool
surface, and ``invoke_agent``. For each call it writes one ``audit_log``
row unless the call already wrote its own record:

* The row is written synchronously, before the tool result goes back to
  the model. If the write fails, the tool call fails and so does the run
  (audit is fail-closed).
* ``agent_session_id`` is the run id
  (:func:`~meho_backplane.operations._audit.resolve_agent_session_id`). For
  a child run started by ``invoke_agent`` it is the child's run id:
  :mod:`meho_backplane.agent.invoke` binds it around the child loop.
* A call that committed its own classified row for the same caller (a
  dispatch through ``call_operation``) gets no second row. The check uses
  the same record scope the REST and MCP wrappers use
  (:mod:`meho_backplane.audit_cutover`). Each row this module writes gets
  its own scope, so a child run's row never counts as the record of the
  parent's ``invoke_agent`` call.
* The payload stores the tool name, a hash of the arguments, the op class,
  and for a failure the error type. It never stores the arguments or the
  error text. The hash is the keyed audit hash the MCP rows use, with
  secret-named values left out.
* A call to a tool name the run cannot use never reaches a tool hook:
  Pydantic AI rejects the name first and asks the model to retry. The
  ``after_model_request`` hook records the attempt from the model's
  response (400, ``UnknownToolError``). The tool is not enabled; the model
  still gets the framework's retry message.

Event class: ``legacy_request`` while ``AUDIT_WRAPPER_ROWS_RETIRED`` is
off, like every other wrapper row. With it on, a refused call is a
``policy_decision`` and any other call is an ``operation_execution``. An
agent run has no request usage event to fall back on, so routine agent
calls keep their row after the cutover.

Status codes follow the MCP wrapper rows: 200 for success, 403 for a call
the toolset refused (:class:`ToolRefusedRetry`), 400 for bad arguments, a
retry the tool asked for, or a call to a tool name the run cannot use, 500
for an error.
"""

from __future__ import annotations

import contextlib
import json
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog
from pydantic import ValidationError
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.capabilities import (
    AbstractCapability,
    RawToolArgs,
    ValidatedToolArgs,
    WrapToolExecuteHandler,
)
from pydantic_ai.exceptions import ToolFailedError, ToolRetryError
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.tools import ToolDefinition

from meho_backplane.audit_cutover import (
    classified_record_seen,
    close_classified_record_scope,
    open_classified_record_scope,
    wrapper_rows_retired,
)
from meho_backplane.auth.delegation import resolve_actor_sub
from meho_backplane.auth.operator import Operator
from meho_backplane.broadcast.events import resolve_op_class
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditEventClass, AuditLog
from meho_backplane.mcp.registry import get_tool
from meho_backplane.operations._audit import (
    _apply_contextvar_lineage_mirrors,
    parent_audit_id_var,
    resolve_agent_session_id,
    work_ref_var,
)
from meho_backplane.operations._validate import compute_audit_params_hash

__all__ = [
    "AGENT_TOOL_AUDIT_METHOD",
    "ToolCallAudit",
    "ToolRefusedRetry",
    "UnknownToolError",
    "write_agent_tool_row",
]

_log = structlog.get_logger(__name__)

#: ``audit_log.method`` for an agent tool call row.
AGENT_TOOL_AUDIT_METHOD = "AGENT"

#: ``audit_log.path`` prefix; the tool name follows.
_PATH_PREFIX = "/agent/tools/call/"


class ToolRefusedRetry(ModelRetry):
    """A :class:`ModelRetry` that refuses a tool call (role or connector gate).

    It reaches the model like any retry. The audit row records it as a
    refusal (403), not as bad input.
    """


class UnknownToolError(LookupError):
    """The model called a tool name this run cannot use.

    Pydantic AI rejects the name before any tool hook runs, so the audit
    row names this type. The error itself is never raised.
    """


def _params_hash(args: Any) -> str:
    """The audit hash of *args*; same as the MCP ``params_hash`` for the same arguments.

    Raw arguments arrive as JSON text when the model's call could not be
    validated; that text is parsed first so secret-named values are left
    out too. Text that is not JSON is hashed as one string.
    """
    if isinstance(args, str):
        with contextlib.suppress(ValueError):
            args = json.loads(args)
    return compute_audit_params_hash(args)


def _op_class(tool_name: str) -> str:
    """Return the op class for *tool_name*, using the MCP tool's class when there is one."""
    entry = get_tool(tool_name)
    return resolve_op_class(tool_name, entry[0].op_class if entry is not None else None)


def _row_class(status_code: int) -> AuditEventClass:
    """Pick the event class for an agent tool call row."""
    if not wrapper_rows_retired():
        return AuditEventClass.LEGACY_REQUEST
    if status_code in (401, 403):
        return AuditEventClass.POLICY_DECISION
    return AuditEventClass.OPERATION_EXECUTION


def _cause(error: Exception) -> Exception:
    """Return the tool's own exception.

    Pydantic AI turns a :class:`ModelRetry` raised by the tool into a
    :class:`ToolRetryError` (and a ``ToolFailed`` into a
    :class:`ToolFailedError`) before the wrapper sees it; the tool's
    exception is the ``__cause__``.
    """
    if isinstance(error, (ToolRetryError, ToolFailedError)) and isinstance(
        error.__cause__, Exception
    ):
        return error.__cause__
    return error


def _status_for(error: BaseException) -> int:
    if isinstance(error, (ToolRetryError, ToolFailedError)):
        return 400
    if isinstance(error, ToolRefusedRetry):
        return 403
    if isinstance(error, (ModelRetry, ValidationError)):
        return 400
    return 500


async def write_agent_tool_row(
    *,
    operator: Operator,
    tool_name: str,
    args: Any,
    status_code: int,
    duration_ms: float,
    error: BaseException | None = None,
) -> uuid.UUID:
    """Commit one ``audit_log`` row for an agent tool call. Raises on failure."""
    payload: dict[str, Any] = {
        "op_id": tool_name,
        "params_hash": _params_hash(args),
        "op_class": _op_class(tool_name),
    }
    if error is not None:
        payload["error_type"] = type(error).__name__
    if operator.name:
        payload["principal_name"] = operator.name
    _apply_contextvar_lineage_mirrors(payload)
    audit_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        session.add(
            AuditLog(
                id=audit_id,
                event_class=_row_class(status_code),
                occurred_at=datetime.now(UTC),
                operator_sub=operator.sub,
                actor_sub=resolve_actor_sub(),
                tenant_id=operator.tenant_id,
                agent_session_id=resolve_agent_session_id(),
                parent_audit_id=parent_audit_id_var.get(),
                work_ref=work_ref_var.get(),
                method=AGENT_TOOL_AUDIT_METHOD,
                path=f"{_PATH_PREFIX}{tool_name}",
                status_code=status_code,
                duration_ms=Decimal(str(round(duration_ms, 3))),
                payload=payload,
            )
        )
        await session.commit()
    return audit_id


class ToolCallAudit(AbstractCapability[Operator]):
    """Write one audit row per agent tool call. See the module docstring."""

    async def wrap_tool_execute(
        self,
        ctx: RunContext[Operator],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: ValidatedToolArgs,
        handler: WrapToolExecuteHandler,
    ) -> Any:
        start = time.monotonic()
        status_code = 500
        error: Exception | None = None
        scope = open_classified_record_scope()
        try:
            try:
                result = await handler(args)
            except Exception as exc:
                error = _cause(exc)
                status_code = _status_for(error)
                raise
            status_code = 200
            return result
        finally:
            try:
                if not classified_record_seen(ctx.deps.sub):
                    await self._write(ctx.deps, tool_def.name, args, status_code, start, error)
            finally:
                close_classified_record_scope(scope)

    async def after_model_request(
        self,
        ctx: RunContext[Operator],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        # A call to a tool name this run cannot use never reaches the tool
        # hooks: Pydantic AI rejects the name first and asks the model to
        # retry. Record the attempt from the response, so a run that keeps
        # calling a missing tool still shows what it tried.
        if ctx.tool_manager is None or ctx.tool_manager.tools is None:
            # The step's tools are not resolved, so no name can be checked.
            return response
        for part in response.parts:
            if isinstance(part, ToolCallPart) and not ctx.is_tool_available(part.tool_name):
                await self._write(
                    ctx.deps,
                    part.tool_name,
                    part.args,
                    400,
                    time.monotonic(),
                    UnknownToolError(part.tool_name),
                )
        return response

    async def on_tool_validate_error(
        self,
        ctx: RunContext[Operator],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: RawToolArgs,
        error: ValidationError | ModelRetry,
    ) -> ValidatedToolArgs:
        # The model sent arguments that failed the tool's schema. The tool
        # never ran, but the attempt is still on the record.
        await self._write(ctx.deps, tool_def.name, args, 400, time.monotonic(), error)
        raise error

    @staticmethod
    async def _write(
        operator: Operator,
        tool_name: str,
        args: Any,
        status_code: int,
        start: float,
        error: BaseException | None,
    ) -> None:
        # The row gets its own record scope. A child run started by
        # invoke_agent runs inside the parent's scope, and after the cutover
        # this row has an execution class. Without its own scope, a child's
        # error row would count as the record of the parent's invoke_agent
        # call, and the parent's own row would be skipped.
        scope = open_classified_record_scope()
        try:
            await write_agent_tool_row(
                operator=operator,
                tool_name=tool_name,
                args=args,
                status_code=status_code,
                duration_ms=(time.monotonic() - start) * 1000,
                error=error,
            )
        except Exception:
            # Fail closed: the exception replaces the tool result, so the
            # call (and the run) fails instead of running without a record.
            _log.exception("agent_tool_audit_write_failed", tool=tool_name, status_code=status_code)
            raise
        finally:
            close_classified_record_scope(scope)
