# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The agent meta-tool wiring type shared across the toolset modules.

:class:`MetaToolSpec` is one meta-tool's agent-facing wiring — handler +
schema + role floor — the unit :mod:`meho_backplane.agent.toolset` composes
into the catalog and :mod:`meho_backplane.agent.toolset_broadcast` produces
for the broadcast coordination tools. It lives in its own module so both can
import it without a cycle (the broadcast bridge builds specs; the toolset
resolver consumes them).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any

from pydantic_ai import ToolFailed

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.operations.meta_tools import (
    ConnectorNotIngestedError,
    UnknownConnectorError,
)

__all__ = ["MetaToolSpec", "connector_errors_reach_model"]


#: The shape of a handler the agent meta-tools adapt: the existing
#: ``(operator, arguments) -> dict`` MEHO meta-tool signature shared by the
#: REST, MCP, and CLI surfaces. The adapter repacks the framework's
#: ``RunContext``-first, keyword-only tool call onto this shape.
_MetaToolHandler = Callable[[Operator, dict[str, Any]], Awaitable[dict[str, Any]]]


class MetaToolSpec:
    """One meta-tool's agent-facing wiring: handler + schema + role floor.

    The agent front-end's source of truth for *its* tool surface, parallel
    to the MCP front-end's :func:`~meho_backplane.mcp.registry.register_mcp_tool`
    registrations in :mod:`meho_backplane.mcp.tools.operations`. Both adapt
    the same handlers in :mod:`meho_backplane.operations.meta_tools`; neither
    is a wrapper around the other (the CLAUDE.md dual-front-end contract).

    Fields:

    * ``name`` — the tool name the model sees (matches the MCP tool name so
      a definition author's allow-list reads identically across surfaces).
    * ``handler`` — the ``(operator, arguments) -> dict`` meta-tool handler.
    * ``description`` — the model-facing prompt for *when* to call the tool.
    * ``parameter_schema`` — JSON Schema 2020-12 for the tool's arguments;
      the framework advertises it to the model. The dispatcher re-validates
      ``call_operation`` params against the descriptor's own
      ``parameter_schema`` (defence in depth — the agent-tool schema only
      shapes the meta-tool *arguments*, not the per-op params).
    * ``required_role`` — the :class:`TenantRole` floor; the identity must
      meet it (via :func:`role_at_least`) for the tool to register.
    """

    __slots__ = ("description", "handler", "name", "parameter_schema", "required_role")

    def __init__(
        self,
        *,
        name: str,
        handler: _MetaToolHandler,
        description: str,
        parameter_schema: dict[str, Any],
        required_role: TenantRole,
    ) -> None:
        self.name = name
        self.handler = handler
        self.description = description
        self.parameter_schema = parameter_schema
        self.required_role = required_role


@contextmanager
def connector_errors_reach_model() -> Iterator[None]:
    """Send a bad ``connector_id`` back to the model instead of ending the run.

    The discovery handlers raise :class:`UnknownConnectorError` or
    :class:`ConnectorNotIngestedError` when the model names a connector that
    does not exist or has no operations yet (#4232). Any other exception from
    a tool ends the whole run, so this turns those two into
    :class:`pydantic_ai.ToolFailed`. The model sees a failed tool result with
    the same text the MCP surface returns, and can try another id. A
    ``ToolFailed`` does not use the tool's retry budget; the run's
    ``request_limit`` still bounds the loop.
    """
    try:
        yield
    except (UnknownConnectorError, ConnectorNotIngestedError) as exc:
        raise ToolFailed(str(exc)) from exc
