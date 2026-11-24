# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Stop a hosted agent run when its person is blocked.

:class:`PrincipalBlockGate` is a Pydantic AI capability. The run loop
passes it to every agent it builds
(:meth:`~meho_backplane.agent.run.PydanticAgentRun._build_agent`), so it
sees every tool call (the meta-tools, the two default tools and
``invoke_agent``) and every model request of the run and of its child runs.

Before each tool call and before each model request it reads the admin
block list of :mod:`meho_backplane.auth.principal_block`, with the same
check and the same refusal text as an operation call
(:func:`~meho_backplane.operations._principal_block_gate.refuse_if_principal_blocked`).
When the run's person is blocked, or the list cannot be read, it raises
:class:`PrincipalBlockedRunError`. That is not a retry: the tool does not
run, the model gets no further request, and the run ends failed with the
refusal text.
"""

from __future__ import annotations

from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import (
    AbstractCapability,
    ValidatedToolArgs,
    WrapToolExecuteHandler,
)
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.tools import ToolDefinition

from meho_backplane.auth.operator import Operator
from meho_backplane.operations._principal_block_gate import (
    UNAVAILABLE_ERROR_CODE,
    refuse_if_principal_blocked,
)

__all__ = ["PrincipalBlockGate", "PrincipalBlockedRunError"]

_MODEL_REQUEST = "model_request"


class PrincipalBlockedRunError(Exception):
    """The run's person is blocked, or the block list cannot be read.

    *status_code* is what the tool call's audit row records: 403 for a
    block, 503 when the list cannot be read.
    """

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


async def _stop_if_blocked(operator: Operator, step: str) -> None:
    refusal = await refuse_if_principal_blocked(step, operator.sub, operator.tenant_id)
    if refusal is None:
        return
    unavailable = refusal.extras.get("error_code") == UNAVAILABLE_ERROR_CODE
    raise PrincipalBlockedRunError(refusal.error or "", status_code=503 if unavailable else 403)


class PrincipalBlockGate(AbstractCapability[Operator]):
    """Refuse every tool call and model request of a blocked person. See the module docstring."""

    async def before_model_request(
        self,
        ctx: RunContext[Operator],
        request_context: ModelRequestContext,
    ) -> ModelRequestContext:
        await _stop_if_blocked(ctx.deps, _MODEL_REQUEST)
        return request_context

    async def wrap_tool_execute(
        self,
        ctx: RunContext[Operator],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: ValidatedToolArgs,
        handler: WrapToolExecuteHandler,
    ) -> Any:
        await _stop_if_blocked(ctx.deps, tool_def.name)
        return await handler(args)
