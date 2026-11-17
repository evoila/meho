# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The call flow guide in the session-start text (#4715).

These tests check:

* the full guide is at most 450 tokens;
* every tool the guide names exists, and each step lists the tools it names;
* a step is left out when the session cannot see its tools, and a session
  without ``call_operation`` gets no guide;
* with the ``MCP_CALL_FLOW_GUIDE`` switch off, the session-start text does
  not change;
* with the switch on, the text starts with the guide, and the guide names
  only tools on the session's tool list.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, patch

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.conventions.preamble import PreambleAssembly
from meho_backplane.conventions.schemas import estimate_tokens
from meho_backplane.mcp.call_flow_guide import (
    CALL_FLOW_BLOCK_END,
    CALL_FLOW_BLOCK_START,
    CALL_FLOW_STEPS,
    call_flow_guide,
)
from meho_backplane.mcp.registry import all_tools_for, get_tool
from meho_backplane.mcp.server import _session_instructions
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

MAX_GUIDE_TOKENS = 450

#: Every tool any step names.
ALL_GUIDE_TOOLS: frozenset[str] = frozenset().union(*(tools for tools, _ in CALL_FLOW_STEPS))

_CODE = re.compile(r"`([^`]+)`")
_STEP_LINE = re.compile(r"^(\d+)\. ", re.MULTILINE)


def _operator(role: TenantRole, *, profile: str | None = None) -> Operator:
    return Operator(
        sub="op-call-flow-guide",
        raw_jwt="fixture-jwt-not-real",
        tenant_id=OPERATOR_TENANT_ID,
        tenant_role=role,
        mcp_tool_profile=profile,
    )


def _named_tools(text: str) -> set[str]:
    """The registered tools that *text* names in backticks."""
    return {name for name in _CODE.findall(text) if get_tool(name) is not None}


def _set_switch(monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    monkeypatch.setenv("MCP_CALL_FLOW_GUIDE", "true" if on else "false")
    get_settings.cache_clear()


async def _instructions(operator: Operator) -> str | None:
    preamble = PreambleAssembly(
        text="",
        dropped_slugs=[],
        kept_slugs=[],
        token_counts={},
        conventions_tokens=0,
        oversize_slugs=[],
    )
    with (
        patch(
            "meho_backplane.conventions.preamble.assemble_preamble_detailed",
            new=AsyncMock(return_value=preamble),
        ),
        patch("meho_backplane.recall.memory_band", new=AsyncMock(return_value="")),
    ):
        return await _session_instructions(operator)


def test_full_guide_fits_the_token_limit() -> None:
    guide = call_flow_guide(ALL_GUIDE_TOOLS)
    assert estimate_tokens(guide) <= MAX_GUIDE_TOKENS
    assert len(_STEP_LINE.findall(guide)) == len(CALL_FLOW_STEPS)


def test_every_named_tool_exists() -> None:
    for name in ALL_GUIDE_TOOLS:
        assert get_tool(name) is not None, name


@pytest.mark.parametrize(("tools", "text"), CALL_FLOW_STEPS)
def test_each_step_lists_the_tools_it_names(tools: frozenset[str], text: str) -> None:
    assert _named_tools(text) <= tools
    # The approval step is about the answer of ``call_operation``; it names no tool.
    assert tools <= set(_CODE.findall(text)) or tools == {"call_operation"}


def test_no_guide_without_call_operation() -> None:
    assert call_flow_guide(ALL_GUIDE_TOOLS - {"call_operation"}) == ""
    assert call_flow_guide(set()) == ""


def test_a_step_without_its_tools_is_left_out() -> None:
    guide = call_flow_guide(ALL_GUIDE_TOOLS - {"preview_operation", "list_targets"})
    assert "preview_operation" not in guide
    assert "meho_connector_list" not in guide
    assert "list_targets" not in guide
    numbers = [int(n) for n in _STEP_LINE.findall(guide)]
    assert numbers == list(range(1, len(CALL_FLOW_STEPS) - 1))
    assert guide.startswith(CALL_FLOW_BLOCK_START)
    assert guide.endswith(CALL_FLOW_BLOCK_END)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [TenantRole.READ_ONLY, TenantRole.OPERATOR])
async def test_switch_off_leaves_the_text_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    role: TenantRole,
) -> None:
    _set_switch(monkeypatch, on=False)
    off = await _instructions(_operator(role))
    monkeypatch.delenv("MCP_CALL_FLOW_GUIDE")
    get_settings.cache_clear()
    unset = await _instructions(_operator(role))
    assert off == unset
    assert off is not None
    assert CALL_FLOW_BLOCK_START not in off


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "profile"),
    [
        (TenantRole.READ_ONLY, None),
        (TenantRole.OPERATOR, None),
        (TenantRole.TENANT_ADMIN, None),
        (TenantRole.OPERATOR, "investigate"),
        (TenantRole.OPERATOR, "operate"),
    ],
)
async def test_switch_on_names_only_listed_tools(
    monkeypatch: pytest.MonkeyPatch,
    role: TenantRole,
    profile: str | None,
) -> None:
    _set_switch(monkeypatch, on=False)
    without = await _instructions(_operator(role, profile=profile))
    _set_switch(monkeypatch, on=True)
    operator = _operator(role, profile=profile)
    instructions = await _instructions(operator)
    assert instructions is not None
    listed = {defn.name for defn in all_tools_for(operator)}
    guide = call_flow_guide(listed)
    if "call_operation" not in listed:
        assert instructions == without
        return
    assert instructions == f"{guide}\n\n{without}"
    assert _named_tools(guide) <= listed


@pytest.mark.asyncio
async def test_operator_session_gets_every_step(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_switch(monkeypatch, on=True)
    instructions = await _instructions(_operator(TenantRole.OPERATOR))
    assert instructions is not None
    assert instructions.startswith(call_flow_guide(ALL_GUIDE_TOOLS))


@pytest.mark.asyncio
async def test_investigate_profile_has_no_preview_step(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_switch(monkeypatch, on=True)
    instructions = await _instructions(_operator(TenantRole.OPERATOR, profile="investigate"))
    assert instructions is not None
    assert instructions.startswith(CALL_FLOW_BLOCK_START)
    assert "call_operation" in instructions
    assert "preview_operation" not in instructions
