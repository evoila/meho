# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``MCP_STRICT_TOOL_SCHEMAS``: no ``null`` inside an ``enum`` in the sent tool schemas.

A strict model gateway refuses a tool schema that holds ``null`` inside an
``enum``, and then refuses the whole tool list. With the switch on, the copy
of each input schema that ``tools/list`` sends has no such ``null``. The
schema the server checks a call against does not change.

The tests read the tools from the registry. They keep no list of tools.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from scripts.snapshot_mcp_tools import render_snapshot

from meho_backplane.agent import toolset_discovery
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


def _switch(monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    monkeypatch.setenv("MCP_STRICT_TOOL_SCHEMAS", "true" if on else "false")
    get_settings.cache_clear()


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


def test_switch_is_off_by_default() -> None:
    assert get_settings().mcp_strict_tool_schemas is False


def test_the_registry_has_null_enums_to_remove() -> None:
    """The tests below mean something only while some tool has this shape."""
    assert _null_enum_fields()


def test_switch_on_no_sent_schema_has_null_in_an_enum(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch(monkeypatch, on=True)
    bad = {
        definition.name: _null_enum_paths(definition.to_wire()["inputSchema"])
        for definition in _registered()
    }
    assert {name: paths for name, paths in bad.items() if paths} == {}


def test_switch_off_every_sent_schema_is_the_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """The snapshot is built from ``to_wire()``, so this checks every sent schema."""
    _switch(monkeypatch, on=False)
    assert _SNAPSHOT.read_text(encoding="utf-8") == render_snapshot()
    assert _null_enum_paths(json.loads(_SNAPSHOT.read_text(encoding="utf-8")))


def test_switch_on_changes_only_the_null_enum_members(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch(monkeypatch, on=True)
    for definition in _registered():
        sent = definition.to_wire()["inputSchema"]
        expected = json.loads(json.dumps(_wire_safe_input_schema(definition.inputSchema)))
        assert sent == _without_null_enum_members(expected), definition.name


def test_the_server_schema_keeps_the_null_members(monkeypatch: pytest.MonkeyPatch) -> None:
    before = {d.name: json.dumps(d.inputSchema, sort_keys=True) for d in _registered()}
    _switch(monkeypatch, on=True)
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
        "anyOf": [{"enum": [None]}],
    }
    out = _without_null_enum_members(schema)
    assert out["properties"]["a"]["items"]["enum"] == ["x"]
    assert out["$defs"]["d"]["properties"]["b"] == {"type": ["string", "null"], "enum": ["y"]}
    assert out["anyOf"] == [{"enum": []}]
    assert schema["properties"]["a"]["items"]["enum"] == ["x", None]


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


async def test_a_call_with_null_or_without_the_field_passes_and_gives_the_same_result(
    monkeypatch: pytest.MonkeyPatch,
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    _switch(monkeypatch, on=True)
    operator = build_operator(TenantRole.TENANT_ADMIN, platform_admin=True)
    fields = _null_enum_fields()
    assert len(fields) == 16
    for tool, field in fields:
        definition, _handler = mcp_registry._TOOLS[tool]
        seen: list[Any] = []

        async def _recorder(
            _operator: Operator,
            arguments: dict[str, Any],
            _field: str = field,
            _seen: list[Any] = seen,
        ) -> dict[str, Any]:
            # The handlers read the field with ``arguments.get``, so a missing
            # field and ``null`` are the same value for them.
            _seen.append(arguments.get(_field))
            return {"field": arguments.get(_field)}

        monkeypatch.setitem(mcp_registry._TOOLS, tool, (definition, _recorder))
        base = _arguments(definition)
        base.pop(field, None)

        left_out = await handle_tools_call(operator, {"name": tool, "arguments": dict(base)})
        sent_null = await handle_tools_call(
            operator, {"name": tool, "arguments": {**base, field: None}}
        )

        assert left_out["isError"] is False, (tool, field)
        assert sent_null["isError"] is False, (tool, field)
        assert left_out["content"] == sent_null["content"], (tool, field)
        assert seen == [None, None], (tool, field)


async def test_a_value_outside_the_enum_is_still_refused(
    monkeypatch: pytest.MonkeyPatch,
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    _switch(monkeypatch, on=True)
    tool, field = _null_enum_fields()[0]
    definition, _handler = mcp_registry._TOOLS[tool]
    operator = build_operator(TenantRole.TENANT_ADMIN, platform_admin=True)
    arguments = {**_arguments(definition), field: "not-a-member"}
    with pytest.raises(McpInvalidParamsError):
        await handle_tools_call(operator, {"name": tool, "arguments": arguments})


def _hosted_agent_schemas() -> dict[str, dict[str, Any]]:
    specs = (
        *toolset_discovery.build_discovery_meta_tools(),
        *toolset_discovery.build_retrieval_meta_tools(),
        *toolset_discovery.build_write_meta_tools(),
        *toolset_discovery.build_docs_meta_tools(),
    )
    return {spec.name: spec.parameter_schema for spec in specs}


def test_hosted_agent_tool_list_has_no_null_in_an_enum_when_the_switch_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch(monkeypatch, on=True)
    schemas = _hosted_agent_schemas()
    assert {"search_knowledge", "search_memory"} <= set(schemas)
    assert {name: _null_enum_paths(s) for name, s in schemas.items() if _null_enum_paths(s)} == {}


def test_hosted_agent_tool_list_keeps_the_null_when_the_switch_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch(monkeypatch, on=False)
    assert _null_enum_paths(_hosted_agent_schemas()["search_knowledge"])
