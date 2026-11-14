# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Short tool texts behind the ``MCP_SHORT_TOOL_TEXTS`` switch (#4172).

These tests check:

* the switch is off by default, and with it off every listed tool is the
  same as before the short texts existed;
* with it on, each tool with a short text is shorter, shows that text with
  its maturity label, and accepts exactly the same inputs (the schema
  without its ``description`` strings is unchanged);
* with it on, no other tool changes;
* every input path of a short text names a real input of its tool;
* the short ``add_to_knowledge`` text still follows ``KB_SIMILAR_CHECK``;
* the short texts hold no issue numbers or planning codes;
* the helper never changes the registered tool.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from scripts.generate_reference_docs import _INTERNAL_REF

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp import registry as mcp_registry
from meho_backplane.mcp.handlers import handle_tools_list
from meho_backplane.mcp.registry import MCP_ADMIN_SCOPE, ToolDefinition, ToolSurface
from meho_backplane.mcp.short_texts import (
    ShortText,
    apply_short_text,
    input_property,
    with_short_text,
)
from meho_backplane.mcp.tools import _short_tool_texts as texts
from meho_backplane.mcp.tools._connector_classification import classification_for_settings
from meho_backplane.mcp.tools._kb_similar_text import similar_check_for_settings
from meho_backplane.mcp.tools._operation_recall import recall_schema_for_settings
from meho_backplane.mcp.tools._search_classification import classification_schema_for_settings
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

#: Every tool with a short text, and its text.
SHORT_TEXTS: dict[str, ShortText] = {
    "query_topology": texts.QUERY_TOPOLOGY_SHORT,
    "meho_runbook_next": texts.RUNBOOK_NEXT_SHORT,
    "add_to_knowledge": texts.ADD_TO_KNOWLEDGE_SHORT,
    "result_query": texts.RESULT_QUERY_SHORT,
    "preview_operation": texts.PREVIEW_OPERATION_SHORT,
    "read_docs": texts.READ_DOCS_SHORT,
    "search_operations": texts.SEARCH_OPERATIONS_SHORT,
    "meho_broadcast_recent": texts.BROADCAST_RECENT_SHORT,
    "call_operation": texts.CALL_OPERATION_SHORT,
    "meho_broadcast_announce": texts.BROADCAST_ANNOUNCE_SHORT,
    "search_knowledge": texts.SEARCH_KNOWLEDGE_SHORT,
    "list_targets": texts.LIST_TARGETS_SHORT,
    "search_docs": texts.SEARCH_DOCS_SHORT,
    "meho_broadcast_watch": texts.BROADCAST_WATCH_SHORT,
}

#: The other switches, all off and all on.
_OTHER_SWITCHES = [pytest.param(False, id="other-switches-off"), pytest.param(True, id="on")]


def _set_switches(monkeypatch: pytest.MonkeyPatch, *, short: bool, others: bool) -> None:
    monkeypatch.setenv("MCP_SHORT_TOOL_TEXTS", "true" if short else "false")
    monkeypatch.setenv("KB_SIMILAR_CHECK", "true" if others else "false")
    monkeypatch.setenv("TEAM_KNOWLEDGE_RECALL", "true" if others else "false")
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true" if others else "false")
    get_settings.cache_clear()


def _operator() -> Operator:
    """An elevated tenant admin with the docs capability: every tool lists."""
    return Operator(
        sub="op-short-texts",
        raw_jwt="fixture-jwt-not-real",
        tenant_id=OPERATOR_TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
        capabilities=frozenset({"meho-docs"}),
        scopes=frozenset({MCP_ADMIN_SCOPE}),
    )


async def _listed() -> dict[str, dict[str, Any]]:
    result = await handle_tools_list(_operator(), None)
    return {tool["name"]: tool for tool in result["tools"]}


def _registered(name: str) -> ToolDefinition:
    return mcp_registry._TOOLS[name][0]


def _before(name: str) -> dict[str, Any]:
    """The tool as it was listed before the short texts, under the current settings."""
    definition = _registered(name)
    if name == "add_to_knowledge":
        definition = similar_check_for_settings(definition)
    if name in {"call_operation", "preview_operation"}:
        definition = recall_schema_for_settings(definition)
    if name == "search_operations":
        definition = classification_schema_for_settings(definition)
    if name in {"meho_connector_edit_op", "meho_connector_edit_group"}:
        definition = classification_for_settings(definition, _operator())
    return definition.to_wire()


def _size(tool: dict[str, Any]) -> int:
    schema = json.dumps(tool["inputSchema"], separators=(",", ":"), ensure_ascii=False)
    return len(tool["name"]) + len(tool["description"]) + len(schema)


def _without_descriptions(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _without_descriptions(v) for k, v in node.items() if k != "description"}
    if isinstance(node, list):
        return [_without_descriptions(v) for v in node]
    return node


def _label(name: str) -> str:
    """The maturity label the registry puts before the tool's description."""
    description = _registered(name).description
    return description[: description.index("] ") + 2] if description.startswith("[") else ""


def test_switch_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_SHORT_TOOL_TEXTS", raising=False)
    get_settings.cache_clear()
    assert get_settings().mcp_short_tool_texts is False


@pytest.mark.parametrize("others", _OTHER_SWITCHES)
async def test_switch_off_lists_every_tool_as_before(
    monkeypatch: pytest.MonkeyPatch, others: bool
) -> None:
    _set_switches(monkeypatch, short=False, others=others)
    listed = await _listed()
    assert set(SHORT_TEXTS) <= set(listed)
    for name, tool in listed.items():
        assert tool == _before(name), f"{name} changed with the switch off"


@pytest.mark.parametrize("others", _OTHER_SWITCHES)
async def test_switch_on_each_changed_tool_is_shorter_with_the_same_inputs(
    monkeypatch: pytest.MonkeyPatch, others: bool
) -> None:
    _set_switches(monkeypatch, short=True, others=others)
    listed = await _listed()
    for name, short in SHORT_TEXTS.items():
        tool, before = listed[name], _before(name)
        assert _size(tool) < _size(before), name
        assert tool["description"] == _label(name) + short.description
        assert _without_descriptions(tool["inputSchema"]) == _without_descriptions(
            before["inputSchema"]
        ), f"{name} accepts other inputs with the switch on"
        assert {k: v for k, v in tool.items() if k not in {"description", "inputSchema"}} == {
            k: v for k, v in before.items() if k not in {"description", "inputSchema"}
        }


async def test_switch_on_changes_no_other_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_switches(monkeypatch, short=True, others=False)
    listed = await _listed()
    changed = {name for name, tool in listed.items() if tool != _before(name)}
    assert changed == set(SHORT_TEXTS)


def test_short_texts_are_for_working_surface_tools() -> None:
    for name in SHORT_TEXTS:
        assert _registered(name).surface is ToolSurface.WORKING, name


@pytest.mark.parametrize("name", sorted(SHORT_TEXTS))
def test_every_short_input_is_an_input_of_its_tool(name: str) -> None:
    schema = _registered(name).inputSchema
    missing = [path for path in SHORT_TEXTS[name].inputs if input_property(schema, path) is None]
    assert missing == []


def test_short_texts_hold_no_internal_refs() -> None:
    pattern = re.compile(_INTERNAL_REF)
    found = {name: pattern.findall(str(short)) for name, short in SHORT_TEXTS.items()}
    assert {name: hits for name, hits in found.items() if hits} == {}


async def test_short_add_to_knowledge_follows_the_similar_check_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the check on, ``create_anyway`` carries the check's short text; off, no word of it."""
    _set_switches(monkeypatch, short=True, others=True)
    on = (await _listed())["add_to_knowledge"]
    create_anyway = on["inputSchema"]["properties"]["create_anyway"]
    assert create_anyway["description"] == texts.ADD_TO_KNOWLEDGE_SHORT.inputs["create_anyway"]
    assert "similar entries" in create_anyway["description"]

    _set_switches(monkeypatch, short=True, others=False)
    listed = await _listed()
    for name, tool in listed.items():
        text = str(tool)
        for word in ("create_anyway", "similar entries", "similar_entries", "team_knowledge"):
            assert word not in text, f"{name} lists {word!r} with its switch off"


# ---------------------------------------------------------------------------
# The helper on a made-up tool
# ---------------------------------------------------------------------------


def _tool(description: str = "Long text.", feature: str | None = None) -> ToolDefinition:
    return ToolDefinition(
        name="made_up_tool",
        description=description,
        feature=feature,
        surface=ToolSurface.WORKING,
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "A long name text."},
                "query": {
                    "type": "object",
                    "description": "A long query text.",
                    "properties": {
                        "filter": {
                            "type": "array",
                            "description": "A long filter text.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "value": {"description": "A long value text."},
                                },
                            },
                        },
                    },
                },
            },
            "required": ["name"],
        },
    )


def test_input_property_walks_objects_and_arrays() -> None:
    schema = _tool().inputSchema
    assert input_property(schema, "name") == schema["properties"]["name"]
    assert input_property(schema, "query.filter.value") == {"description": "A long value text."}
    assert input_property(schema, "query.missing") is None
    assert input_property(schema, "name.deeper") is None
    assert input_property(schema, "") is None


def test_apply_short_text_sets_drops_and_skips_without_changing_the_tool() -> None:
    tool = _tool()
    kept = json.dumps(tool.inputSchema, sort_keys=True)
    short = ShortText(
        description="Short.",
        inputs={"name": "Name.", "query": None, "query.filter.value": "V.", "gone": "X."},
    )
    shown = apply_short_text(tool, short)
    props = shown.inputSchema["properties"]
    assert shown.description == "Short."
    assert props["name"]["description"] == "Name."
    assert "description" not in props["query"]
    assert props["query"]["properties"]["filter"]["items"]["properties"]["value"] == {
        "description": "V."
    }
    assert "gone" not in props
    assert json.dumps(tool.inputSchema, sort_keys=True) == kept
    assert tool.description == "Long text."


def test_apply_short_text_keeps_the_maturity_label() -> None:
    labelled = mcp_registry._with_maturity_prefix(_tool(feature="topology"))
    assert labelled.description.startswith("[")
    shown = apply_short_text(labelled, ShortText(description="Short."))
    assert shown.description == labelled.description.split("] ")[0] + "] Short."


def test_with_short_text_runs_the_other_step_first_in_both_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def drop_name(definition: ToolDefinition) -> ToolDefinition:
        schema = {**definition.inputSchema, "properties": {"query": {"type": "object"}}}
        return definition.model_copy(update={"inputSchema": schema})

    for_settings = with_short_text(
        ShortText(description="Short.", inputs={"name": "Name."}), then=drop_name
    )
    for short in (False, True):
        monkeypatch.setenv("MCP_SHORT_TOOL_TEXTS", "true" if short else "false")
        get_settings.cache_clear()
        shown = for_settings(_tool(), _operator())
        assert "name" not in shown.inputSchema["properties"]
        assert shown.description == ("Short." if short else "Long text.")
