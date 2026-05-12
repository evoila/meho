# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho.status`` — the reference MCP tool (G0.5-T4).

Mirrors :func:`~meho_backplane.api.v1.health.authenticated_health` onto
the MCP transport. A call to ``tools/call`` with
``name="meho.status"`` returns the same operator-identity +
Vault-federation + DB-migration bundle the chassis ``GET /api/v1/health``
route returns, scoped to the operator the MCP dispatcher already
authenticated.

Why this is the *reference* tool
================================

If the four-step chain ``GET /api/v1/health`` exercises (JWT validation
→ Vault JWT/OIDC login → KV v2 read of ``meho/test/federation`` → DB
migration probe) works end-to-end for the MCP transport, every other
downstream tool (G3 cluster, G4 knowledge, G5 memory, G6 broadcast, G7
conventions, G8 targets) inherits a working auth + dispatch baseline.
A failure surfaces here loudly *before* any product tool would.

The tool description matters
============================

Per AI-engineering best-practices, the ``description`` field is the
agent's prompt for *when to call this tool*. Imprecise descriptions
get tools called incorrectly, miss-routed, or never invoked at all. The
description below names what the tool does, when an agent should call
it (session start / connectivity check), and that there are no
arguments — copy-paste good for downstream connector tools.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.api.v1.health import build_health_response
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool


async def _meho_status_handler(
    operator: Operator,
    _arguments: dict[str, Any],
) -> dict[str, Any]:
    """Return the chassis ``/api/v1/health`` payload wire-identical.

    The MCP dispatcher already validated ``arguments`` against the
    tool's ``inputSchema`` (``{additionalProperties: false}``) before
    reaching this handler, so the body unconditionally builds the
    health bundle. ``model_dump(mode="json")`` serialises the
    :class:`HealthResponse` to a JSON-safe dict that the dispatcher
    wraps in the MCP ``content`` array.
    """
    response = await build_health_response(operator)
    return response.model_dump(mode="json")


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.status",
        description=(
            "Returns the operator's identity (sub, name, email) plus the "
            "MEHO backplane's dependency status: Vault federation chain "
            "(reachable + KV read OK?) and DB migration state. Call at "
            "MCP session start to verify the operator can reach all "
            "subsystems before issuing product calls. No arguments."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        required_role=TenantRole.READ_ONLY,
        op_class="read",
    ),
    handler=_meho_status_handler,
)
