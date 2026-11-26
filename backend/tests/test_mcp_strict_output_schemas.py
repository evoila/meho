# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""No ``null`` inside an ``enum`` in the sent tool output schemas.

A strict model gateway refuses a tool schema that holds ``null`` inside an
``enum``. A client may also check each answer against the sent output schema.
MEHO answers ``null`` for some enum fields (``delivery`` of ``call_operation``).
So the sent output schema must hold no ``null`` inside an ``enum`` and must
still accept ``null`` there.

The tests read the tools from the registry. They keep no list of tools.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.mcp import registry as mcp_registry
from meho_backplane.mcp.registry import (
    ToolDefinition,
    _wire_safe_output_schema,
    _without_null_enum_members,
)
from meho_backplane.mcp.tools._call_operation_async import _envelope
from tests.mcp_test_fixtures import (
    client_with_operator,  # noqa: F401 -- pytest-discovered fixture
    isolated_registry,  # noqa: F401 -- pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 -- pytest-discovered autouse fixture
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
                found.append(path or "/")
            found.extend(_null_enum_paths(value, f"{path}/{key}"))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found.extend(_null_enum_paths(item, f"{path}/{index}"))
    return found


def _call_operation() -> ToolDefinition:
    return next(d for d in _registered() if d.name == "call_operation")


def _sent_output_schema() -> dict[str, Any]:
    schema = _call_operation().to_wire()["outputSchema"]
    assert isinstance(schema, dict)
    return schema


def test_the_registry_has_a_null_enum_in_an_output_schema() -> None:
    """The tests below mean something only while some output schema has this shape."""
    assert any(
        _null_enum_paths(d.outputSchema) for d in _registered() if d.outputSchema is not None
    )


def test_no_sent_output_schema_has_null_in_an_enum() -> None:
    bad = {
        definition.name: _null_enum_paths(definition.to_wire()["outputSchema"])
        for definition in _registered()
        if definition.outputSchema is not None
    }
    assert {name: paths for name, paths in bad.items() if paths} == {}


def test_the_snapshot_has_no_null_in_an_enum_of_an_output_schema() -> None:
    tools = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))["tools"]
    assert not [tool["name"] for tool in tools if _null_enum_paths(tool.get("outputSchema", {}))]


def test_the_server_output_schema_is_not_changed() -> None:
    assert _null_enum_paths(_call_operation().outputSchema)
    _call_operation().to_wire()
    assert _null_enum_paths(_call_operation().outputSchema)


def _answers() -> dict[str, dict[str, Any]]:
    """Every kind of ``call_operation`` answer, in the shape MEHO sends."""
    audit_id = uuid.uuid4()

    def dumped(**fields: Any) -> dict[str, Any]:
        return OperationResult(op_id="x.y", duration_ms=1.5, **fields).model_dump(mode="json")

    return {
        "inline": dumped(status="ok", result={"a": 1}, audit_id=audit_id, delivery="complete"),
        "partial": dumped(status="ok", audit_id=audit_id, delivery="partial"),
        "unavailable": dumped(status="ok", audit_id=audit_id, delivery="unavailable"),
        "delivery_null": dumped(status="ok", result={"a": 1}),
        "error_delivery_null": dumped(status="error", error="boom"),
        "waits": _envelope(
            "x.y", status="pending", duration_ms=3.0, extras={"run_id": str(uuid.uuid4())}
        ),
    }


@pytest.mark.parametrize("kind", sorted(_answers()))
def test_every_kind_of_answer_passes_the_sent_output_schema(kind: str) -> None:
    """The check the MCP Python SDK runs on structured content (``jsonschema.validate``)."""
    jsonschema.validate(_answers()[kind], _sent_output_schema())


def test_an_answer_outside_the_enum_is_still_refused() -> None:
    answer = {**_answers()["inline"], "delivery": "sometimes"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(answer, _sent_output_schema())


def test_dropping_only_null_from_the_enum_would_refuse_an_answer() -> None:
    """The plain copy (as for input schemas) refuses ``delivery`` null, so it is not used."""
    plain = _without_null_enum_members(_call_operation().outputSchema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_answers()["delivery_null"], plain)


def test_the_rewrite_keeps_the_other_keywords_and_accepts_null() -> None:
    schema = {
        "type": ["string", "null"],
        "enum": ["a", "b", None],
        "description": "text",
    }
    sent = _wire_safe_output_schema(schema)
    assert sent["description"] == "text"
    assert not _null_enum_paths(sent)
    for good in ("a", "b", None):
        jsonschema.validate(good, sent)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate("c", sent)


def test_the_rewrite_keeps_an_anyof_the_schema_already_has() -> None:
    schema = {"anyOf": [{"type": "string"}, {"type": "null"}], "enum": ["a", None]}
    sent = _wire_safe_output_schema(schema)
    assert not _null_enum_paths(sent)
    for good in ("a", None):
        jsonschema.validate(good, sent)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate("c", sent)


def test_the_rewrite_leaves_an_enum_without_null_alone() -> None:
    schema = {"type": "string", "enum": ["a", "b"]}
    assert _wire_safe_output_schema(schema) is schema


def test_the_rewrite_goes_to_any_depth_and_leaves_values_alone() -> None:
    schema = {
        "type": "object",
        "properties": {
            "enum": {"type": "string"},
            "inner": {
                "type": "array",
                "items": {"enum": ["x", None], "default": {"enum": [None]}},
            },
        },
    }
    sent = _wire_safe_output_schema(schema)
    assert not _null_enum_paths(sent["properties"]["inner"]["items"]["anyOf"])
    assert sent["properties"]["inner"]["items"]["default"] == {"enum": [None]}
    assert sent["properties"]["enum"] == {"type": "string"}


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_tools_list_over_the_mcp_path_has_no_null_in_an_enum(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``POST /mcp`` ``tools/list``: no input or output schema has ``null`` in an ``enum``."""
    client, _op = client_with_operator
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = response.json()["result"]["tools"]
    assert any("outputSchema" in tool for tool in tools)
    bad = {
        tool["name"]: _null_enum_paths(
            {"in": tool["inputSchema"], "out": tool.get("outputSchema", {})}
        )
        for tool in tools
    }
    assert {name: paths for name, paths in bad.items() if paths} == {}
    call_operation = next(tool for tool in tools if tool["name"] == "call_operation")
    jsonschema.validate(_answers()["delivery_null"], call_operation["outputSchema"])
