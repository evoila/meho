# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho_status`` — the reference MCP tool (G0.5-T4).

Mirrors :func:`~meho_backplane.api.v1.health.authenticated_health` onto
the MCP transport. A call to ``tools/call`` with
``name="meho_status"`` returns the same operator-identity +
Vault-federation + DB-migration bundle the chassis ``GET /api/v1/health``
route returns, scoped to the operator the MCP dispatcher already
authenticated.

The tool is open to every role, but the Vault check inside it is not.
The check logs in to Vault as the caller and reads a secret, so it runs
only for a caller with at least the ``operator`` role, the same rule as
``GET /api/v1/health``. A ``read_only`` caller gets every other part of
the answer and a ``vault`` block with ``checked=false``.

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
from meho_backplane.mcp.registry import (
    READ_ONLY,
    ToolDefinition,
    ToolSurface,
    register_mcp_tool,
)


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
        # Deliberately unclassified (#2675): the reference tool mirrors
        # /api/v1/health — infrastructure, not a classified feature
        # (the _READY_ENTRY_FEATURE ``mcp``-entry precedent).
        feature=None,
        name="meho_status",
        annotations=READ_ONLY,
        surface=ToolSurface.WORKING,
        description=(
            "Returns the operator's identity (sub, name, email, tenant_id, "
            "tenant_role) and MEHO's dependency status: Vault (reachable + "
            "KV read OK?), the database's revision and the one this build "
            "expects, the /ready checks (name, ok, detail), the approval "
            "settings (approval key state, whether a second person must "
            "approve), the gated features and the settings they still need, "
            "this session's OAuth scopes, the working-surface tools it should "
            "see versus the ones it lists, and the server's time in UTC "
            "(server_time; use it when you must write down the time). Call at "
            "MCP session start to check that MEHO reaches all subsystems "
            "before product calls, or after an upgrade to confirm the "
            "database reached the new revision. No arguments."
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
