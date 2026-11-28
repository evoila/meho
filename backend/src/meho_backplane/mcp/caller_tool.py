# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The MCP client name on the rows of a personal agent's MCP call (``CUSTOM_POLICY``).

An MCP client sends its name once, in ``initialize`` (``clientInfo.name``).
With ``CUSTOM_POLICY`` on, that call writes the ``mcp.session_open`` row of
the new session, which keeps the name. A later call of the session reads
the name from that row (:func:`~meho_backplane.auth.caller_record.session_tool_name`).
:func:`bind_tool_name` binds the name for the rows of the request, so their
``caller`` block names the tool
(:func:`~meho_backplane.auth.caller_record.apply_caller_block`). It is what
the tool says about itself; MEHO decides nothing with it.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog

from meho_backplane.auth.caller_kind import caller_kind
from meho_backplane.auth.caller_record import (
    bind_mcp_tool_name,
    caller_record_on,
    session_tool_name,
)
from meho_backplane.auth.delegation import resolve_actor_sub
from meho_backplane.auth.operator import Operator

__all__ = ["bind_tool_name"]


async def bind_tool_name(method: str, params: dict[str, Any] | None, operator: Operator) -> None:
    """Bind the MCP client name of this request, for a personal agent only."""
    if not caller_record_on() or caller_kind(operator, resolve_actor_sub()) != "personal_agent":
        return
    if method == "initialize":
        client_info = (params or {}).get("clientInfo")
        if isinstance(client_info, dict):
            bind_mcp_tool_name(client_info.get("name"))
        return
    raw = structlog.contextvars.get_contextvars().get("mcp_session_id")
    if not isinstance(raw, str):
        return
    bind_mcp_tool_name(await session_tool_name(operator, uuid.UUID(raw)))
