# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""No ``null`` inside an ``enum`` in the sent tool schemas.

A strict model gateway refuses a tool schema that holds ``null`` inside an
``enum``, and then refuses the whole tool list. The copy of each input schema
that ``tools/list`` sends has no such ``null``. The schema the server checks a
call against does not change.

The tests read the tools from the registry. They keep no list of tools.
"""

from __future__ import annotations

import importlib
import inspect
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import jsonschema
import pytest
from pydantic import BaseModel
from scripts.snapshot_mcp_tools import render_snapshot

from meho_backplane.agent.invoke import make_invoke_agent_tool
from meho_backplane.agent.toolset import INVOKE_AGENT, META_TOOL_NAMES, resolve_agent_toolset
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp import registry as mcp_registry
from meho_backplane.mcp.handlers import handle_tools_call
from meho_backplane.mcp.registry import (
    ToolDefinition,
    _wire_safe_input_schema,
    _without_null_enum_members,
)
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import (
    build_operator,
    isolated_registry,  # noqa: F401 -- pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 -- pytest-discovered autouse fixture
    seeded_operator_tenant,  # noqa: F401 -- pytest-discovered fixture
)

_SNAPSHOT = Path(__file__).resolve().parents[1] / "contracts" / "mcp-tools-list.json"


def _registered() -> list[ToolDefinition]:
    return [definition for definition, _handler in mcp_registry._TOOLS.values()]


def _null_enum_paths(node: Any, path: str = "") -> list[str]:
    """The path of every ``enum`` that holds ``None``, at any depth."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "enum" and isinstance(value, list) and None in value:
                found.append(f"{path}/enum")
            found.extend(_null_enum_paths(value, f"{path}/{key}"))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found.extend(_null_enum_paths(item, f"{path}/{index}"))
    return found


def _null_enum_fields() -> list[tuple[str, str]]:
    """``(tool, field)`` for each top-level input whose ``enum`` holds ``None``."""
    fields: list[tuple[str, str]] = []
    for definition in _registered():
        properties = definition.inputSchema.get("properties", {})
        for field, schema in properties.items():
            if isinstance(schema, dict) and None in (schema.get("enum") or []):
                fields.append((definition.name, field))
    return sorted(fields)


def test_the_registry_has_null_enums_to_remove() -> None:
    """The tests below mean something only while some tool has this shape."""
    assert _null_enum_fields()


def test_no_sent_schema_has_null_in_an_enum() -> None:
    bad = {
        definition.name: _null_enum_paths(definition.to_wire()["inputSchema"])
        for definition in _registered()
    }
    assert {name: paths for name, paths in bad.items() if paths} == {}


def test_the_snapshot_is_the_sent_schemas_and_has_no_null_in_an_enum() -> None:
    """The snapshot is built from ``to_wire()``, so this checks every sent schema."""
    assert _SNAPSHOT.read_text(encoding="utf-8") == render_snapshot()
    tools = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))["tools"]
    assert not [tool["name"] for tool in tools if _null_enum_paths(tool["inputSchema"])]


def test_only_the_null_enum_members_change() -> None:
    for definition in _registered():
        sent = definition.to_wire()["inputSchema"]
        expected = json.loads(json.dumps(_wire_safe_input_schema(definition.inputSchema)))
        assert sent == _without_null_enum_members(expected), definition.name


def test_the_server_schema_keeps_the_null_members() -> None:
    before = {d.name: json.dumps(d.inputSchema, sort_keys=True) for d in _registered()}
    for definition in _registered():
        definition.to_wire()
    assert {d.name: json.dumps(d.inputSchema, sort_keys=True) for d in _registered()} == before
    assert _null_enum_fields()


def test_a_type_list_with_null_stays() -> None:
    schema = {"type": "object", "properties": {"a": {"type": ["string", "null"]}}}
    assert _without_null_enum_members(schema) is schema


def test_nested_enums_lose_null_at_every_depth() -> None:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"a": {"type": "array", "items": {"enum": ["x", None]}}},
        "$defs": {"d": {"properties": {"b": {"type": ["string", "null"], "enum": [None, "y"]}}}},
        "anyOf": [{"enum": [None, "z"]}],
    }
    out = _without_null_enum_members(schema)
    assert out["properties"]["a"]["items"]["enum"] == ["x"]
    assert out["$defs"]["d"]["properties"]["b"] == {"type": ["string", "null"], "enum": ["y"]}
    assert out["anyOf"] == [{"enum": ["z"]}]
    assert schema["properties"]["a"]["items"]["enum"] == ["x", None]


def _accepts(schema: dict[str, Any], value: Any) -> bool:
    return jsonschema.Draft202012Validator(schema).is_valid(value)


@pytest.mark.parametrize(
    ("schema", "sent"),
    [
        ({"enum": [None]}, {"const": None}),
        ({"enum": [None, None]}, {"const": None}),
        ({"type": ["string", "null"], "enum": [None]}, {"type": ["string", "null"], "const": None}),
        ({"const": None, "enum": [None]}, {"const": None}),
        # No value passes both rules, so the sent copy must accept none either.
        ({"const": "x", "enum": [None]}, {"const": "x", "enum": []}),
        ({"enum": [None], "const": "x"}, {"enum": [], "const": "x"}),
    ],
)
def test_an_enum_of_only_null_accepts_the_same_values(
    schema: dict[str, Any], sent: dict[str, Any]
) -> None:
    out = _without_null_enum_members(schema)
    assert out == sent
    for value in (None, "x", "", 23, False, 0, {}, []):
        assert _accepts(out, value) == _accepts(schema, value), value
    nested = {"type": "object", "properties": {"a": schema}, "anyOf": [schema]}
    assert _wire_safe_input_schema(nested) == {"type": "object", "properties": {"a": sent}}


@pytest.mark.parametrize("schema", [{"enum": []}, {"enum": ["x"]}, {"enum": [False, 0, ""]}])
def test_an_enum_without_null_stays(schema: dict[str, Any]) -> None:
    assert _without_null_enum_members(schema) is schema
    nested = {"type": "object", "properties": {"a": schema, "enum": schema}}
    assert _without_null_enum_members(nested) is nested


def test_an_enum_member_is_a_value_and_stays_as_it_is() -> None:
    literal = {"enum": ["x", None], "properties": {"a": {"enum": [None]}}}
    array = [{"enum": ["y", None]}, None]
    only_values: dict[str, Any] = {"enum": [literal, array]}
    assert _without_null_enum_members(only_values) is only_values
    out = _without_null_enum_members({"enum": [literal, None, array]})
    assert out == {"enum": [literal, array]}
    assert _accepts(out, literal)
    assert _accepts(out, array)
    assert not _accepts(out, {"enum": ["x"], "properties": {"a": {}}})


_D = {"d": {"enum": ["x", None]}}

_ORDER_SENSITIVE: list[dict[str, Any]] = [
    {"not": {"enum": ["x", None]}},
    {"not": {"enum": [None]}},
    {"not": {"not": {"enum": ["x", None]}}},
    {"if": {"enum": ["x", None]}, "then": {"type": "string"}, "else": {"type": "null"}},
    {"if": {"type": "string"}, "then": {"enum": ["x", None]}},
    {"if": {"type": "null"}, "else": {"enum": ["x", None]}},
    {"properties": {"a": {"not": {"enum": ["x", None]}}}},
    # A ``$ref`` from ``not`` or ``if`` to an enum in ``$defs``.
    {"properties": {"a": {"not": {"$ref": "#/$defs/d"}}}, "$defs": _D},
    {"properties": {"a": {"not": {"$ref": "#/definitions/d"}}}, "definitions": _D},
    {
        "if": {"$ref": "#/$defs/d"},
        "then": {"type": "string"},
        "else": {"type": "null"},
        "$defs": _D,
    },
    # A ``$ref`` that reaches the enum through a second definition.
    {
        "properties": {"a": {"not": {"$ref": "#/$defs/e"}}},
        "$defs": {"e": {"$ref": "#/$defs/d"}, **_D},
    },
    # A ``$ref`` form whose target is not a plain path.
    {"not": {"$ref": "#here"}, "$defs": {"d": {"$anchor": "here", "enum": ["x", None]}}},
    # ``null`` matches both branches, so ``oneOf`` refuses it.
    {"properties": {"a": {"oneOf": [{"enum": ["x", None]}, {"type": "null"}]}}},
    {"then": {"oneOf": [{"enum": ["x", None]}, {"type": "null"}]}},
    {"properties": {"a": {"oneOf": [{"$ref": "#/$defs/d"}, {"type": "null"}]}}, "$defs": _D},
    # Two matches are too many, one is not.
    {"contains": {"enum": ["x", None]}, "maxContains": 1},
]


@pytest.mark.parametrize("schema", _ORDER_SENSITIVE)
def test_the_sent_copy_never_accepts_what_the_full_schema_refuses(schema: dict[str, Any]) -> None:
    sent = _without_null_enum_members(schema)
    for value in (None, "x", "other", 3, [None, "x"]):
        for candidate in (value, {"a": value}):
            if _accepts(sent, candidate):
                assert _accepts(schema, candidate), (schema, candidate)


@pytest.mark.parametrize(
    "schema",
    [
        {"properties": {"a": {"not": {"$ref": "#/$defs/d"}}}, "$defs": _D},
        {"properties": {"a": {"oneOf": [{"enum": ["x", None]}, {"type": "null"}]}}},
        {"if": {"type": "null"}, "then": {"oneOf": [{"enum": ["x", None]}, {"type": "null"}]}},
    ],
)
def test_null_is_still_refused_through_a_ref_or_a_one_of(schema: dict[str, Any]) -> None:
    candidate = None if "if" in schema else {"a": None}
    assert not _accepts(schema, candidate)
    assert not _accepts(_without_null_enum_members(schema), candidate)


def test_an_enum_under_not_if_and_one_of_is_left_as_it_is() -> None:
    for schema in _ORDER_SENSITIVE:
        if "if" not in schema or "$defs" in schema:
            assert _without_null_enum_members(schema) is schema, schema
    narrowed = _without_null_enum_members({"if": {"type": "string"}, "then": {"enum": ["x", None]}})
    assert narrowed == {"if": {"type": "string"}, "then": {"enum": ["x"]}}


def test_a_definition_used_only_outside_not_and_if_still_loses_null() -> None:
    schema = {
        "properties": {"a": {"$ref": "#/$defs/d"}, "b": {"not": {"$ref": "#/$defs/k"}}},
        "$defs": {**_D, "k": {"enum": ["y", None]}},
    }
    out = _without_null_enum_members(schema)
    assert out["$defs"] == {"d": {"enum": ["x"]}, "k": {"enum": ["y", None]}}
    assert _without_null_enum_members({"contains": {"enum": ["x", None]}}) == {
        "contains": {"enum": ["x"]}
    }


def test_a_schema_that_keeps_null_in_an_enum_is_found_by_the_check() -> None:
    sent = _wire_safe_input_schema({"type": "object", "not": {"enum": ["x", None]}})
    assert _null_enum_paths(sent) == ["/not/enum"]


def test_an_id_key_in_a_default_does_not_hide_a_later_id() -> None:
    """``$id`` counts only as a schema keyword, so the one of ``choice`` keeps it all."""
    schema = {
        "properties": {
            "a": {"default": {"$id": None}},
            "choice": {
                "$id": "urn:example:choice",
                "$defs": {"e": {"enum": ["x", None]}},
                "not": {"$ref": "#/$defs/e"},
            },
        }
    }
    sent = _without_null_enum_members(schema)
    assert not _accepts(schema, {"choice": None})
    assert not _accepts(sent, {"choice": None})
    assert sent is schema


_B = {"b": {"enum": ["x", None]}}


@pytest.mark.parametrize(
    "a",
    [
        {"not": {"const": {"$ref": "book"}}},
        {"not": {"default": {"$ref": "book"}}},
        {"not": {"properties": {"$ref": {"type": "string"}}}},
    ],
)
def test_a_ref_key_that_is_data_or_a_name_is_not_followed(a: dict[str, Any]) -> None:
    schema = {"properties": {"a": a, **_B}}
    sent = _without_null_enum_members(schema)
    assert sent["properties"]["a"] == a
    assert sent["properties"]["b"] == {"enum": ["x"]}


_NAME_KEY_VALUES: list[Any] = [
    {},
    {"b": None},
    {"b": "x"},
    {"$id": 1},
    {"$id": 1, "b": None},
    {"$id": 1, "b": "x"},
    {"a": {"$ref": 1}},
    {"a": {"$ref": 1, "b": "x"}},
    {"a": {}, "b": None},
]


@pytest.mark.parametrize(
    ("name_map", "value"),
    [
        ("dependentRequired", {"$id": ["b"]}),
        ("dependencies", {"$id": {"type": "object"}}),
        ("$vocabulary", {"$id": True}),
    ],
)
def test_a_key_of_a_name_map_is_a_name_and_not_an_id(name_map: str, value: Any) -> None:
    schema = {"properties": _B, name_map: value}
    sent = _without_null_enum_members(schema)
    assert sent["properties"]["b"] == {"enum": ["x"]}
    assert sent[name_map] == value
    for candidate in _NAME_KEY_VALUES:
        if _accepts(sent, candidate):
            assert _accepts(schema, candidate), candidate


def test_a_ref_key_under_dependent_required_inside_not_is_a_name() -> None:
    a = {"not": {"dependentRequired": {"$ref": ["b"]}}}
    schema = {"properties": {"a": a, **_B}}
    sent = _without_null_enum_members(schema)
    assert sent["properties"]["a"] == a
    assert sent["properties"]["b"] == {"enum": ["x"]}
    for candidate in _NAME_KEY_VALUES:
        if _accepts(sent, candidate):
            assert _accepts(schema, candidate), candidate


def _sample(schema: dict[str, Any]) -> Any:
    """A valid value for *schema*, enough for the required inputs."""
    if "enum" in schema:
        return next(member for member in schema["enum"] if member is not None)
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next(member for member in kind if member != "null")
    if kind == "string":
        fmt = schema.get("format")
        if fmt == "uuid":
            return str(uuid.uuid4())
        if fmt == "date-time":
            return "2026-01-01T00:00:00Z"
        return "x" * max(schema.get("minLength", 1), 1)
    if kind in {"integer", "number"}:
        return max(schema.get("minimum", 1), 1)
    if kind == "boolean":
        return True
    if kind == "array":
        return [_sample(schema["items"])] * schema.get("minItems", 0) if "items" in schema else []
    if kind == "object":
        properties = schema.get("properties", {})
        return {name: _sample(properties[name]) for name in schema.get("required", [])}
    raise AssertionError(f"no sample for {schema}")


def _arguments(definition: ToolDefinition) -> dict[str, Any]:
    """Valid arguments: the required inputs, then any a conditional rule asks for."""
    schema = definition.inputSchema
    properties = schema.get("properties", {})
    arguments = {name: _sample(properties[name]) for name in schema.get("required", [])}
    validator = jsonschema.Draft202012Validator(schema)
    for _round in range(5):
        missing = [
            error.message.split("'")[1]
            for error in validator.iter_errors(arguments)
            if error.validator == "required"
        ]
        if not missing:
            break
        for name in missing:
            arguments[name] = _sample(properties.get(name, {"type": "string"}))
    assert not list(validator.iter_errors(arguments)), definition.name
    return arguments


_TOOLS_MODULE = "meho_backplane.mcp.tools"


@dataclass(frozen=True)
class _Below:
    """What stands in for the layer below one real handler.

    *target* is ``(module, attribute path)`` of the service or database call the
    handler makes. *result* is what the stand-in returns: a value, or a function
    of what the stand-in received.
    """

    target: tuple[str, str]
    result: Any


def _echo_override(received: dict[str, Any]) -> SimpleNamespace:
    payload = received["payload"]
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    return SimpleNamespace(
        id=uuid.UUID(int=1),
        tenant_id=uuid.UUID(int=2),
        created_by_sub="s",
        created_at=stamp,
        updated_at=stamp,
        **payload,
    )


def _empty_page() -> SimpleNamespace:
    return SimpleNamespace(model_dump=lambda mode: {"rows": [], "next_cursor": None})


class _ReviewPayload(BaseModel):
    connector_id: str = "x"


class _IngestResult:
    def to_api_models(self) -> tuple[BaseModel, None]:
        return _ReviewPayload(), None


#: Per tool: the smallest valid arguments (without the field under test) and the
#: stand-in below the real handler. A tool with such a field and no entry fails
#: ``test_every_tool_with_a_null_enum_field_has_a_case``.
_CASES: dict[str, tuple[dict[str, Any], _Below]] = {
    "meho_broadcast_overrides_set": (
        {"op_id_pattern": "x", "detail": "full"},
        _Below((f"{_TOOLS_MODULE}.broadcast_overrides", "create_override_impl"), _echo_override),
    ),
    "meho_connector_edit_op": (
        {"connector_id": "x", "op_id": "x", "requires_approval": True},
        _Below((f"{_TOOLS_MODULE}.connector_admin", "ReviewService.edit_op"), []),
    ),
    "meho_connector_enable_reads": (
        {"connector_id": "x"},
        _Below((f"{_TOOLS_MODULE}.connector_admin", "ReviewService.enable_reads"), 3),
    ),
    "meho_connector_ingest": (
        {
            "product": "x",
            "version": "1",
            "impl_id": "x",
            "specs": [{"uri": "https://example.test/spec.yaml"}],
        },
        _Below(
            (f"{_TOOLS_MODULE}.connector_ingest", "IngestionPipelineService.ingest"),
            _IngestResult(),
        ),
    ),
    "meho_connector_review": (
        {"connector_id": "x"},
        _Below(
            (f"{_TOOLS_MODULE}.connector_admin", "ReviewService.get_review_payload"),
            _ReviewPayload(),
        ),
    ),
    "meho_runbook_list_runs": (
        {},
        _Below((f"{_TOOLS_MODULE}.runbook_runs", "RunbookRunService.list_runs"), []),
    ),
    "meho_runbook_list_templates": (
        {},
        _Below((f"{_TOOLS_MODULE}.runbooks", "RunbookTemplateService.list_templates"), []),
    ),
    "query_audit": ({}, _Below((f"{_TOOLS_MODULE}.audit", "query_audit"), _empty_page())),
    "query_topology": (
        {"kind": "edges"},
        _Below((f"{_TOOLS_MODULE}.topology", "list_edges"), []),
    ),
    "search_knowledge": (
        {"query": "x"},
        _Below((f"{_TOOLS_MODULE}.knowledge", "KbService.search_entries"), []),
    ),
    "search_memory": (
        {"query": "x"},
        _Below((f"{_TOOLS_MODULE}.memory", "MemoryService.search_memories"), []),
    ),
}


def _plain(value: Any) -> Any:
    """A comparable form of what a stand-in received."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value


def _stand_in_below(
    monkeypatch: pytest.MonkeyPatch, below: _Below, received: list[dict[str, Any]]
) -> None:
    """Replace the call below the handler. It records what it received, defaults included."""
    module_name, path = below.target
    owner: Any = importlib.import_module(module_name)
    *parents, name = path.split(".")
    for parent in parents:
        owner = getattr(owner, parent)
    signature = inspect.signature(getattr(owner, name))

    async def _stand_in(*args: Any, **kwargs: Any) -> Any:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        arguments = {
            key: _plain(value)
            for key, value in bound.arguments.items()
            if key not in {"self", "session", "operator"}
        }
        received.append(arguments)
        return below.result(arguments) if callable(below.result) else below.result

    monkeypatch.setattr(owner, name, _stand_in)


def test_every_tool_with_a_null_enum_field_has_a_case(
    isolated_registry: None,  # noqa: F811
    required_settings_env: None,  # noqa: F811
) -> None:
    missing = sorted({tool for tool, _field in _null_enum_fields()} - set(_CASES))
    assert not missing, (
        f"tools {missing} have an input with null in an enum, but no entry in _CASES. "
        "Add the smallest valid arguments and the stand-in below the handler."
    )
    stale = sorted(set(_CASES) - {tool for tool, _field in _null_enum_fields()})
    assert not stale, f"_CASES has entries for tools without such a field: {stale}"


async def test_a_call_with_null_or_without_the_field_passes_and_gives_the_same_result(
    monkeypatch: pytest.MonkeyPatch,
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    """Each real handler runs. Only the call below it is a stand-in.

    The same call goes in twice, once without the field and once with ``null``.
    Both must pass the schema check, give the same answer, and hand the same
    arguments to the layer below.
    """
    operator = build_operator(TenantRole.TENANT_ADMIN, platform_admin=True)
    fields = _null_enum_fields()
    assert fields, "the registry has no tool input with null in an enum"
    for tool, field in fields:
        assert tool in _CASES, f"no case for {tool}: add it to _CASES"
        base, below = _CASES[tool]
        assert field not in base, (tool, field)
        runs: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        for arguments in (dict(base), {**base, field: None}):
            received: list[dict[str, Any]] = []
            with monkeypatch.context() as scope:
                _stand_in_below(scope, below, received)
                result = await handle_tools_call(operator, {"name": tool, "arguments": arguments})
            assert result["isError"] is False, (tool, field, result)
            runs.append((result, received))
        (left_out, got_left_out), (sent_null, got_null) = runs
        assert len(got_left_out) == 1, (tool, field, "the real handler did not reach the stand-in")
        assert left_out["content"] == sent_null["content"], (tool, field)
        assert got_left_out == got_null, (tool, field)


def test_literal_values_and_property_names_are_not_schema_rules() -> None:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "default": {"enum": ["a", None]},
            "b": {"default": {"enum": ["x", None]}, "examples": [{"enum": [None, "y"]}]},
            "c": {"const": {"enum": [None]}, "enum": ["z", None]},
        },
    }
    out = _without_null_enum_members(schema)
    assert out["properties"]["default"] == {"enum": ["a"]}
    assert out["properties"]["b"] == schema["properties"]["b"]
    assert out["properties"]["c"] == {"const": {"enum": [None]}, "enum": ["z"]}


async def test_a_value_outside_the_enum_is_still_refused(
    monkeypatch: pytest.MonkeyPatch,
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    tool, field = _null_enum_fields()[0]
    definition, _handler = mcp_registry._TOOLS[tool]
    operator = build_operator(TenantRole.TENANT_ADMIN, platform_admin=True)
    arguments = {**_arguments(definition), field: "not-a-member"}
    with pytest.raises(McpInvalidParamsError):
        await handle_tools_call(operator, {"name": tool, "arguments": arguments})


async def _no_child(**_kwargs: Any) -> Any:
    raise AssertionError("no child agent runs in this test")


def _hosted_agent_schemas() -> dict[str, dict[str, Any]]:
    """The schema of every tool a hosted run can get, all optional tools on."""
    operator = Operator(
        sub="op-agent",
        name="Agent Operator",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=uuid.UUID(int=1),
        tenant_role=TenantRole.OPERATOR,
        capabilities=frozenset({"meho-docs"}),
    )
    resolved = resolve_agent_toolset({"meta_tools": sorted(META_TOOL_NAMES)}, operator)
    assert not resolved.missing
    assert not resolved.disabled
    # The run adds this one itself when the definition names it.
    invoke_tool = make_invoke_agent_tool(resolver=_no_child, child_runner=_no_child)
    tools = [*resolved.tools, invoke_tool]
    return {tool.name: tool.tool_def.parameters_json_schema for tool in tools}


@pytest.mark.parametrize("typed_first", ["false", "true"])
@pytest.mark.parametrize("from_mcp", ["false", "true"])
def test_hosted_agent_tool_list_has_no_null_in_an_enum(
    monkeypatch: pytest.MonkeyPatch, from_mcp: str, typed_first: str
) -> None:
    """Both settings pick another form of the operation tools, so each form is checked."""
    monkeypatch.setenv("AGENT_OPERATION_TOOLS_FROM_MCP", from_mcp)
    monkeypatch.setenv("SEARCH_TYPED_FIRST", typed_first)
    get_settings.cache_clear()
    schemas = _hosted_agent_schemas()
    assert set(schemas) == {*META_TOOL_NAMES, INVOKE_AGENT}
    assert {name: _null_enum_paths(s) for name, s in schemas.items() if _null_enum_paths(s)} == {}
