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

With the ``MCP_SHORT_TOOL_TEXTS`` switch on, the biggest tools send a
short text (:mod:`meho_backplane.mcp.short_texts`), and the whole list
must stay under the lower limit :data:`MAX_CHARS_TOTAL_SHORT`.

A session that asks for a tool profile (#4172) with the short texts
(#4714) gets a lean list. Here the whole ``tools/list`` entry counts, as
UTF-8 bytes of compact JSON: name, description, input schema and output
schema (the short texts send none), so the limit holds for every client,
also one that shows the output schema to the model. Each lean profile
must stay under :data:`MAX_BYTES_LEAN_PROFILE` (6,000 tokens at 4 bytes
per token) and ``query_topology`` under :data:`MAX_BYTES_QUERY_TOPOLOGY`.

If a test here fails, shorten the text: keep what the tool is for, when to
call it, its arguments and the rules that stop wrong calls. Move the rest
to the error text the tool returns, or to ``docs/codebase/mcp.md``. Raise
a limit only with a reason in the pull request.
"""

from __future__ import annotations

import json

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.profiles import TOOL_PROFILES
from meho_backplane.mcp.registry import ToolDefinition, ToolSurface, all_tools_for
from meho_backplane.settings import get_settings
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

#: Most characters the whole default tool list may use with the
#: ``MCP_SHORT_TOOL_TEXTS`` switch on. The list used 46,067 when this limit
#: was set, against 67,865 with the switch off (32% less).
MAX_CHARS_TOTAL_SHORT = 47_000

#: With the switch on, the list must be at least this much smaller.
MIN_SHORT_SAVING = 0.30

#: Most bytes a tool profile's whole list may use with the short texts:
#: 6,000 tokens at 4 bytes per token. When this limit was set, ``investigate``
#: used 14,340 bytes and ``operate`` 21,243 (down from 34,494 and 49,620).
MAX_BYTES_LEAN_PROFILE = 24_000

#: Most bytes the ``query_topology`` entry may use with the short texts.
#: It used 3,272 when this limit was set, down from 13,034.
MAX_BYTES_QUERY_TOPOLOGY = 6_000


def tool_text_size(definition: ToolDefinition) -> int:
    """Return the characters a tool's text uses in a ``tools/list`` answer."""
    wire = definition.to_wire()
    schema = json.dumps(wire["inputSchema"], separators=(",", ":"), ensure_ascii=False)
    return len(wire["name"]) + len(wire["description"]) + len(schema)


def tools_over_limit(definitions: list[ToolDefinition], limit: int) -> dict[str, int]:
    """Return the size of every tool above *limit*, by name."""
    sizes = {d.name: tool_text_size(d) for d in definitions}
    return {name: size for name, size in sizes.items() if size > limit}


def tool_wire_bytes(definition: ToolDefinition) -> int:
    """Return the UTF-8 bytes of the tool's whole ``tools/list`` entry as compact JSON."""
    wire = json.dumps(definition.to_wire(), separators=(",", ":"), ensure_ascii=False)
    return len(wire.encode("utf-8"))


def _default_session_tools(profile: str | None = None) -> list[ToolDefinition]:
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
        mcp_tool_profile=profile,
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


def test_short_texts_make_the_default_list_at_least_30_percent_smaller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_SHORT_TOOL_TEXTS", "false")
    get_settings.cache_clear()
    full = sum(tool_text_size(d) for d in _default_session_tools())
    monkeypatch.setenv("MCP_SHORT_TOOL_TEXTS", "true")
    get_settings.cache_clear()
    short_tools = _default_session_tools()
    short = sum(tool_text_size(d) for d in short_tools)
    assert short <= MAX_CHARS_TOTAL_SHORT, (
        f"default tool list with short texts uses {short} characters, "
        f"limit {MAX_CHARS_TOTAL_SHORT}. Shorten the largest tools."
    )
    assert short <= full * (1 - MIN_SHORT_SAVING), (full, short)
    assert not tools_over_limit(short_tools, MAX_CHARS_PER_TOOL)


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


def _short_texts(monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    monkeypatch.setenv("MCP_SHORT_TOOL_TEXTS", "true" if on else "false")
    get_settings.cache_clear()


@pytest.mark.parametrize("profile", sorted(TOOL_PROFILES))
def test_lean_profile_stays_under_6000_tokens(
    monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    _short_texts(monkeypatch, on=True)
    tools = _default_session_tools(profile)
    assert {d.name for d in tools} == TOOL_PROFILES[profile]
    total = sum(tool_wire_bytes(d) for d in tools)
    assert total <= MAX_BYTES_LEAN_PROFILE, (
        f"profile {profile!r} with short texts uses {total} bytes, "
        f"limit {MAX_BYTES_LEAN_PROFILE} (6,000 tokens). Shorten the largest tools."
    )


def test_short_query_topology_stays_under_6000_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    _short_texts(monkeypatch, on=True)
    topology = next(d for d in _default_session_tools() if d.name == "query_topology")
    size = tool_wire_bytes(topology)
    assert size <= MAX_BYTES_QUERY_TOPOLOGY, (
        f"query_topology with short texts uses {size} bytes, limit {MAX_BYTES_QUERY_TOPOLOGY}."
    )


def test_wire_bytes_count_the_output_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the switch off, the output schema is sent and counted, so the check catches it."""
    _short_texts(monkeypatch, on=False)
    topology = next(d for d in _default_session_tools() if d.name == "query_topology")
    assert topology.outputSchema is not None
    without = topology.model_copy(update={"outputSchema": None})
    schema = json.dumps(topology.outputSchema, separators=(",", ":"), ensure_ascii=False)
    assert tool_wire_bytes(topology) > tool_wire_bytes(without) + len(schema.encode("utf-8"))
    assert tool_wire_bytes(topology) > MAX_BYTES_QUERY_TOPOLOGY
