# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Size limits for the tool text on the default MCP tool list (#4112).

Every MCP session gets the default tool list. Its tool text (name,
description and input schema) is sent to the model before the user's
question, so it uses up the model's context window. A small model often
has a 32,768-token window, so this text must stay short.

The size of one tool is the length of its name, plus its description, plus
its input schema as compact JSON, taken from
:meth:`~meho_backplane.mcp.registry.ToolDefinition.to_wire` (what
``tools/list`` sends). Output schemas are not counted.

If a test here fails, shorten the text: keep what the tool is for, when to
call it, its arguments and the rules that stop wrong calls. Move the rest
to the error text the tool returns, or to ``docs/codebase/mcp.md``. Raise
a limit only with a reason in the pull request.
"""

from __future__ import annotations

import json

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.registry import ToolDefinition, ToolSurface, all_tools_for
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

#: Most characters one default-list tool may use. The largest tool,
#: ``query_topology``, used 5,537 when this limit was set.
MAX_CHARS_PER_TOOL = 6_000

#: Most characters the whole default tool list may use (about 17,500
#: tokens at 4 characters per token). The list used 66,092 when this limit
#: was set, down from 96,804.
MAX_CHARS_TOTAL = 70_000


def tool_text_size(definition: ToolDefinition) -> int:
    """Return the characters a tool's text uses in a ``tools/list`` answer."""
    wire = definition.to_wire()
    schema = json.dumps(wire["inputSchema"], separators=(",", ":"), ensure_ascii=False)
    return len(wire["name"]) + len(wire["description"]) + len(schema)


def tools_over_limit(definitions: list[ToolDefinition], limit: int) -> dict[str, int]:
    """Return the size of every tool above *limit*, by name."""
    sizes = {d.name: tool_text_size(d) for d in definitions}
    return {name: size for name, size in sizes.items() if size > limit}


def _default_session_tools() -> list[ToolDefinition]:
    """Return the tools a default session lists (docs add-on on, no ``mcp:admin``)."""
    operator = Operator(
        sub="op-test",
        name="Test",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=OPERATOR_TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
        capabilities=frozenset({"meho-docs"}),
        scopes=frozenset(),
    )
    return all_tools_for(operator)


def test_default_list_is_the_working_surface() -> None:
    """The measured list is the default working surface, docs tools included."""
    tools = _default_session_tools()
    assert tools
    assert all(d.surface is ToolSurface.WORKING for d in tools)
    assert {"query_topology", "call_operation", "search_docs"} <= {d.name for d in tools}


def test_each_default_tool_stays_under_the_limit() -> None:
    over = tools_over_limit(_default_session_tools(), MAX_CHARS_PER_TOOL)
    assert not over, (
        f"tool text longer than {MAX_CHARS_PER_TOOL} characters: {over}. "
        "Shorten it; see this file's docstring."
    )


def test_whole_default_list_stays_under_the_limit() -> None:
    total = sum(tool_text_size(d) for d in _default_session_tools())
    assert total <= MAX_CHARS_TOTAL, (
        f"default tool list uses {total} characters, limit {MAX_CHARS_TOTAL}. "
        "Shorten the largest tools; see this file's docstring."
    )


@pytest.mark.parametrize("extra", [1, 500])
def test_check_fails_when_a_description_grows_past_the_limit(extra: int) -> None:
    """The check flags a tool as soon as it is longer than the limit."""
    base = next(d for d in _default_session_tools() if d.name == "query_topology")
    room = MAX_CHARS_PER_TOOL - tool_text_size(base)
    grown = base.model_copy(update={"description": base.description + "x" * (room + extra)})
    assert tool_text_size(grown) == MAX_CHARS_PER_TOOL + extra
    assert tools_over_limit([grown], MAX_CHARS_PER_TOOL) == {
        "query_topology": MAX_CHARS_PER_TOOL + extra
    }
    assert tools_over_limit([base], MAX_CHARS_PER_TOOL) == {}
