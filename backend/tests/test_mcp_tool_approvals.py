# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Negative RBAC tests for ``meho.approvals.*`` MCP tools.

G11.2-T5 (#818) registers four approval MCP tools, each declared
with ``required_role=TenantRole.OPERATOR`` on the
:class:`~meho_backplane.mcp.registry.ToolDefinition`. Two gates
enforce that role (same as the agent-grants surface):

* **List-time filter** in
  :func:`~meho_backplane.mcp.registry.all_tools_for`.
* **Call-time re-check** in :mod:`~meho_backplane.mcp.handlers`.

The existing service-layer suite (``test_approval_queue.py``)
exercises
:func:`~meho_backplane.operations.approval_queue.approve_request` /
:func:`~meho_backplane.operations.approval_queue.reject_request`
directly. This file closes the gap by asserting:

* A ``read_only`` role does NOT see any ``meho.approvals.*`` tool in
  the ``tools/list`` response (list-time filter intact).
* A ``read_only`` direct ``tools/call`` against each tool name
  returns the dispatcher's structured rejection — JSON-RPC
  ``-32602`` ``INVALID_PARAMS`` with "forbidden" in the message
  (call-time re-check intact).

Out of scope:

* Happy-path operator coverage — separate task; the re-dispatch +
  audit + broadcast plumbing is exercised by the service-layer
  suite.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator
from meho_backplane.mcp.schemas import INVALID_PARAMS
from tests.mcp_test_fixtures import (
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

#: Every ``meho.approvals.*`` tool registered by
#: :mod:`meho_backplane.mcp.tools.approvals`. Pinning the wire names
#: catches both a rename (test breaks for a missing tool) and a new
#: addition without RBAC review (the matrix below would not exercise
#: the new tool until added here).
_APPROVAL_TOOL_NAMES: tuple[str, ...] = (
    "meho.approvals.list",
    "meho.approvals.get",
    "meho.approvals.approve",
    "meho.approvals.reject",
)


def _tools_call(name: str, arguments: dict[str, Any], call_id: int = 1) -> dict[str, Any]:
    """Build a JSON-RPC ``tools/call`` envelope."""
    return {
        "jsonrpc": "2.0",
        "id": call_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def test_tools_list_hides_approval_tools_from_read_only(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``read_only`` role does NOT see ``meho.approvals.*`` on ``tools/list``.

    Default fixture role is ``read_only``; no parametrize override.
    The list-time filter
    (:func:`~meho_backplane.mcp.registry.all_tools_for`) is the first
    of two RBAC gates. A refactor lowering any approval tool's
    ``required_role`` to ``READ_ONLY`` would surface the tool here
    and fail this assertion.
    """
    client, _op = client_with_operator
    resp = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code == 200, resp.text
    names = {t["name"] for t in resp.json()["result"]["tools"]}
    visible = names & set(_APPROVAL_TOOL_NAMES)
    assert visible == set(), (
        f"read_only role should not see any meho.approvals.* tool; saw {visible!r}"
    )


@pytest.mark.parametrize("tool_name", _APPROVAL_TOOL_NAMES)
def test_read_only_tools_call_approval_is_rejected_with_forbidden(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    tool_name: str,
) -> None:
    """``read_only`` ``tools/call`` against an approval tool → INVALID_PARAMS + "forbidden".

    The call-time re-check in :mod:`~meho_backplane.mcp.handlers` is
    the second RBAC gate — a client that knows the tool's name and
    posts ``tools/call`` directly trips it even though the tool was
    hidden from ``tools/list``. Default fixture role is ``read_only``.
    Arguments are intentionally empty — the role gate fires before
    inputSchema validation.
    """
    client, _op = client_with_operator
    resp = post_mcp(client, _tools_call(tool_name, {}))
    body = resp.json()
    assert "error" in body, body
    assert body["error"]["code"] == INVALID_PARAMS
    assert "forbidden" in body["error"]["message"].lower(), body
