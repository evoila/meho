# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Negative RBAC tests for ``meho.agents.grant.*`` MCP tools.

G11.2-T6 (#819) registers five agent-grant MCP tools, each declared
with ``required_role=TenantRole.TENANT_ADMIN`` on the
:class:`~meho_backplane.mcp.registry.ToolDefinition`. Two gates
enforce that role:

* **List-time filter** in
  :func:`~meho_backplane.mcp.registry.all_tools_for` — tools the
  operator can't call are hidden from ``tools/list``.
* **Call-time re-check** in :mod:`~meho_backplane.mcp.handlers` —
  ``tools/call`` resolves the tool by name and re-verifies
  ``required_role`` before dispatching to the handler. A client that
  knows the tool's name and posts ``tools/call`` directly trips this
  second gate.

The existing service-layer suite (``test_agent_grants.py``) bypasses
both gates by calling
:class:`~meho_backplane.agents.grants.AgentGrantService` directly.
This file closes the gap by asserting:

* An ``operator`` role does NOT see any ``meho.agents.grant.*`` tool
  in the ``tools/list`` response (list-time filter intact).
* An ``operator`` direct ``tools/call`` against each tool name
  returns the dispatcher's structured rejection — JSON-RPC
  ``-32602`` ``INVALID_PARAMS`` with "forbidden" in the message
  (call-time re-check intact).

Out of scope:

* Happy-path tenant-admin coverage — separate task.
* Cross-principal "agent can't grant itself" — independent property.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.schemas import INVALID_PARAMS
from tests.mcp_test_fixtures import (
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

#: Every ``meho.agents.grant.*`` tool registered by
#: :mod:`meho_backplane.mcp.tools.agent_grants`. The list pins the wire
#: names so a rename surfaces as a test break (test_mcp_tool_agent_grants
#: misses the tool) rather than as a silent coverage gap. Paired with the
#: ``required_role=TENANT_ADMIN`` declaration on each
#: :class:`~meho_backplane.mcp.registry.ToolDefinition`.
_GRANT_TOOL_NAMES: tuple[str, ...] = (
    "meho.agents.grant.list",
    "meho.agents.grant.show",
    "meho.agents.grant.create",
    "meho.agents.grant.elevate",
    "meho.agents.grant.revoke",
)


def _tools_call(name: str, arguments: dict[str, Any], call_id: int = 1) -> dict[str, Any]:
    """Build a JSON-RPC ``tools/call`` envelope."""
    return {
        "jsonrpc": "2.0",
        "id": call_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_list_hides_grant_tools_from_operator(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``operator`` role does NOT see ``meho.agents.grant.*`` on ``tools/list``.

    The first of two RBAC gates: the list-time filter in
    :func:`~meho_backplane.mcp.registry.all_tools_for` hides
    TENANT_ADMIN-only tools from operators. A refactor that lowers
    any grant tool's ``required_role`` to ``OPERATOR`` would surface
    the tool here and fail this assertion.
    """
    client, _op = client_with_operator
    resp = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code == 200, resp.text
    names = {t["name"] for t in resp.json()["result"]["tools"]}
    visible = names & set(_GRANT_TOOL_NAMES)
    assert visible == set(), (
        f"operator role should not see any meho.agents.grant.* tool; saw {visible!r}"
    )


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
@pytest.mark.parametrize("tool_name", _GRANT_TOOL_NAMES)
def test_operator_tools_call_grant_is_rejected_with_forbidden(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    tool_name: str,
) -> None:
    """``operator`` ``tools/call`` against a grant tool → INVALID_PARAMS + "forbidden".

    The second of two RBAC gates: the call-time re-check in
    :mod:`~meho_backplane.mcp.handlers` rejects a direct
    ``tools/call`` from below ``tenant_admin``. The arguments dict is
    intentionally empty — the gate fires before the handler's
    inputSchema validation, so we don't need to construct
    schema-valid arguments for every variant.
    """
    client, _op = client_with_operator
    resp = post_mcp(client, _tools_call(tool_name, {}))
    body = resp.json()
    assert "error" in body, body
    assert body["error"]["code"] == INVALID_PARAMS
    assert "forbidden" in body["error"]["message"].lower(), body


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
@pytest.mark.parametrize("tool_name", ["meho.agents.grant.create", "meho.agents.grant.elevate"])
def test_admin_grant_for_unregistered_principal_is_rejected(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    tool_name: str,
) -> None:
    """A tenant-admin grant for an unregistered principal → -32602 naming the scope (#2489).

    Past the RBAC gate, ``AgentGrantService.grant`` rejects a
    ``principal_sub`` that names no non-revoked agent principal in the
    tenant. The MCP boundary maps that :exc:`GrantValidationError` to
    JSON-RPC ``-32602`` ``INVALID_PARAMS``; the message names the
    ``principal_kind=agent`` enforcement scope so the caller learns why a
    grant on a non-agent sub can never fire.
    """
    client, _op = client_with_operator
    arguments: dict[str, Any] = {
        "principal_sub": "never-registered-agent",
        "op_pattern": "*",
        "verdict": "needs-approval",
    }
    if tool_name.endswith("elevate"):
        arguments["expires_at"] = "2099-01-01T00:00:00+00:00"
    resp = post_mcp(client, _tools_call(tool_name, arguments))
    body = resp.json()
    assert "error" in body, body
    assert body["error"]["code"] == INVALID_PARAMS
    assert "principal_kind=agent" in body["error"]["message"], body
