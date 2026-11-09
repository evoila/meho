# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The switch that moves ``query_topology`` off the default MCP tool list.

``MCP_TOPOLOGY_OFF_DEFAULT_LIST`` is off by default, so nothing changes for
existing sessions (the pinned lists in ``test_mcp_surface_conformance.py``
cover that). When an operator turns it on:

* a default session does not list ``query_topology``;
* a call from a default session gets the same answer as a call to any other
  admin tool (it names the ``mcp:admin`` scope), and the handler never runs;
* a session with the ``mcp:admin`` scope lists and calls it;
* the health summary does not report the tool as missing.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest

from meho_backplane.api.v1.health import _surface_summary
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.handlers import handle_tools_call, handle_tools_list
from meho_backplane.mcp.registry import (
    MCP_ADMIN_SCOPE,
    ToolDefinition,
    ToolSurface,
    get_tool,
    tool_surface,
)
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.mcp.tools import topology as topology_tools
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_TOOL = "query_topology"


def _operator(*, elevated: bool = False) -> Operator:
    return Operator(
        sub="op-test",
        name="Test",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=OPERATOR_TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
        capabilities=frozenset(),
        scopes=frozenset({MCP_ADMIN_SCOPE}) if elevated else frozenset(),
    )


@pytest.fixture
def switch_on(monkeypatch: pytest.MonkeyPatch, required_settings_env: None) -> Iterator[None]:  # noqa: F811
    """Turn the switch on for one test."""
    monkeypatch.setenv("MCP_TOPOLOGY_OFF_DEFAULT_LIST", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _listed(operator: Operator) -> set[str]:
    result = await handle_tools_list(operator, None)
    return {tool["name"] for tool in result["tools"]}


def _definition() -> ToolDefinition:
    entry = get_tool(_TOOL)
    assert entry is not None
    return entry[0]


def test_switch_is_off_by_default() -> None:
    assert get_settings().mcp_topology_off_default_list is False
    assert tool_surface(_definition()) is ToolSurface.WORKING


async def test_default_session_lists_the_tool_when_switch_is_off() -> None:
    assert _TOOL in await _listed(_operator())


@pytest.mark.usefixtures("switch_on")
def test_switch_on_moves_only_query_topology() -> None:
    assert tool_surface(_definition()) is ToolSurface.OPERATOR
    # The registered definition itself does not change.
    assert _definition().surface is ToolSurface.WORKING
    other = get_tool("list_targets")
    assert other is not None
    assert tool_surface(other[0]) is ToolSurface.WORKING


@pytest.mark.usefixtures("switch_on")
async def test_switch_on_default_session_does_not_list_the_tool() -> None:
    listed = await _listed(_operator())
    assert _TOOL not in listed
    assert "list_targets" in listed


@pytest.mark.usefixtures("switch_on")
async def test_switch_on_default_session_call_names_the_admin_scope() -> None:
    with pytest.raises(McpInvalidParamsError) as exc_info:
        await handle_tools_call(_operator(), {"name": _TOOL, "arguments": {"kind": "edges"}})
    message = str(exc_info.value)
    assert "operator surface" in message
    assert MCP_ADMIN_SCOPE in message


@pytest.mark.usefixtures("switch_on")
async def test_switch_on_elevated_session_lists_and_calls_the_tool() -> None:
    operator = _operator(elevated=True)
    assert _TOOL in await _listed(operator)
    # The graph read itself needs PostgreSQL; this test checks only that the
    # call gets past the surface check and reaches the tool.
    answer = AsyncMock(return_value={"kind": "edges", "edges": []})
    with patch.object(topology_tools, "_query_topology_answer", answer):
        await handle_tools_call(operator, {"name": _TOOL, "arguments": {"kind": "edges"}})
    answer.assert_awaited_once()


@pytest.mark.usefixtures("switch_on")
async def test_switch_on_health_does_not_report_the_tool_missing() -> None:
    summary = await _surface_summary(_operator())
    assert _TOOL not in summary.expected_working_tools
    assert _TOOL not in summary.listed_working_tools
    assert _TOOL not in summary.missing_working_tools
