# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""MCP tests for the small form of ``result_query`` (``RESULT_QUERY_SMALL_FORM``, #4174).

With the switch off, the tool, its errors and its answers do not change.
With it on: errors name the path of the wrong input, a filter that matches
no row adds ``no_match``, ``contains`` is one more filter operator, and the
tool text stays inside the size budget.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import meho_backplane.operations.result_query as result_query_core
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.registry import ToolSurface, get_tool
from meho_backplane.mcp.schemas import INVALID_PARAMS
from meho_backplane.settings import get_settings
from tests._result_store_helpers import MemoryResultStore
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)
from tests.test_mcp_tool_text_budget import (
    MAX_CHARS_PER_TOOL,
    MAX_CHARS_TOTAL,
    MAX_CHARS_TOTAL_SHORT,
    _default_session_tools,
    tool_text_size,
)

#: Runs a test with an OPERATOR-role client: result_query needs OPERATOR.
_as_operator = pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)

_ROWS: list[dict[str, Any]] = [
    {"name": "web-01", "power_state": "POWERED_ON"},
    {"name": "Web-02", "power_state": "POWERED_OFF"},
    {"name": "db-01", "power_state": "POWERED_ON"},
]


@pytest.fixture
def small_form(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("RESULT_QUERY_SMALL_FORM", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def handle(
    monkeypatch: pytest.MonkeyPatch,
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> str:
    _client, op = client_with_operator
    store = MemoryResultStore()
    handle_id = uuid4()
    store.seed(tenant_id=OPERATOR_TENANT_ID, operator_sub=op.sub, handle_id=handle_id, rows=_ROWS)
    monkeypatch.setattr(result_query_core, "get_result_handle_store", lambda: store)
    return str(handle_id)


def _call(client: TestClient, args: dict[str, Any]) -> dict[str, Any]:
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "result_query", "arguments": args},
        },
    )
    body: dict[str, Any] = response.json()
    return body


def _payload(body: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in body, body
    result = body["result"]
    structured: dict[str, Any] | None = result.get("structuredContent")
    return structured if structured is not None else json.loads(result["content"][0]["text"])


def _listed(client: TestClient) -> dict[str, Any]:
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tool: dict[str, Any] = next(
        t for t in response.json()["result"]["tools"] if t["name"] == "result_query"
    )
    return tool


def _op_schema(tool: dict[str, Any]) -> dict[str, Any]:
    op: dict[str, Any] = tool["inputSchema"]["properties"]["query"]["properties"]["filter"][
        "items"
    ]["properties"]["op"]
    return op


_NO_MATCH = {"filter": [{"field": "power_state", "op": "=", "value": "poweredOff"}]}
_UNKNOWN_FIELD = {"filter": [{"field": "state", "op": "=", "value": "on"}]}
_EXTRA_KEY = {"filter": [{"column": "name", "op": "=", "value": "x"}]}
_PATH_AND_FILTER = {"path": "/a", "filter": [{"field": "name", "op": "=", "value": "x"}]}


# ---------------------------------------------------------------------------
# Switch off: nothing changes
# ---------------------------------------------------------------------------


def test_switch_is_off_by_default() -> None:
    assert get_settings().result_query_small_form is False


@_as_operator
def test_off_the_listed_tool_is_the_registered_tool(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, _op = client_with_operator
    tool = _listed(client)
    found = get_tool("result_query")
    assert found is not None
    registered, _handler = found
    assert tool["inputSchema"] == registered.to_wire()["inputSchema"]
    assert tool["outputSchema"] == registered.outputSchema
    assert "contains" not in _op_schema(tool)["enum"]
    assert "no_match" not in tool["outputSchema"]["properties"]


@_as_operator
def test_off_contains_is_refused_as_before(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    handle: str,
) -> None:
    client, _op = client_with_operator
    query = {"filter": [{"field": "name", "op": "contains", "value": "web"}]}
    body = _call(client, {"handle_id": handle, "query": query})
    assert body["error"]["code"] == INVALID_PARAMS
    assert "query.filter[0].op: one of:" in body["error"]["message"]
    assert "(enum)" in body["error"]["message"]


@_as_operator
def test_off_errors(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    handle: str,
) -> None:
    client, _op = client_with_operator
    unknown = _call(client, {"handle_id": handle, "query": _UNKNOWN_FIELD})["error"]
    assert unknown["message"].startswith("unknown field 'state'")
    assert unknown["data"] == {"reason": "invalid_query"}
    extra = _call(client, {"handle_id": handle, "query": _EXTRA_KEY})["error"]["message"]
    assert extra.endswith(
        'arguments failed inputSchema: query.filter[0]: undeclared property: "column" '
        "(not in the op's declared params) (additionalProperties)"
    )
    spec = _call(client, {"handle_id": handle, "query": _PATH_AND_FILTER})["error"]["message"]
    assert spec.startswith("invalid query spec: 1 validation error(s); first: Value error")


@_as_operator
def test_off_no_match_block_is_absent(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    handle: str,
) -> None:
    client, _op = client_with_operator
    payload = _payload(_call(client, {"handle_id": handle, "query": _NO_MATCH}))
    assert payload["rows"] == []
    assert "no_match" not in payload


# ---------------------------------------------------------------------------
# Switch on
# ---------------------------------------------------------------------------


@_as_operator
def test_on_tool_lists_contains_and_no_match(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    small_form: None,
) -> None:
    client, _op = client_with_operator
    tool = _listed(client)
    op = _op_schema(tool)
    assert op["enum"][-1] == "contains"
    assert "contains" in op["description"]
    assert "no_match" in tool["outputSchema"]["properties"]


def test_on_registered_tool_is_not_changed(small_form: None) -> None:
    """The switch changes the tool a session gets, not the registered one."""
    found = get_tool("result_query")
    assert found is not None
    registered, _handler = found
    assert "contains" not in str(registered.inputSchema)


@_as_operator
def test_on_unknown_field_names_the_path(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    handle: str,
    small_form: None,
) -> None:
    client, _op = client_with_operator
    error = _call(client, {"handle_id": handle, "query": _UNKNOWN_FIELD})["error"]
    assert error["code"] == INVALID_PARAMS
    assert error["message"].startswith("query.filter[0].field: unknown field 'state'")
    assert error["data"] == {"reason": "invalid_query"}


@_as_operator
def test_on_input_check_error_names_the_path(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    handle: str,
    small_form: None,
) -> None:
    client, _op = client_with_operator
    error = _call(client, {"handle_id": handle, "query": _EXTRA_KEY})["error"]
    assert error["code"] == INVALID_PARAMS
    assert error["message"].endswith(
        'arguments failed inputSchema: query.filter[0]: undeclared property: "column" '
        "(not in the op's declared params) (additionalProperties)"
    )
    bad_op = {"filter": [{"field": "name", "op": "LIKE", "value": "x"}]}
    message = _call(client, {"handle_id": handle, "query": bad_op})["error"]["message"]
    assert "inputSchema: query.filter[0].op: one of:" in message
    assert "LIKE" not in message


@_as_operator
def test_on_spec_error_names_the_path(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    handle: str,
    small_form: None,
) -> None:
    client, _op = client_with_operator
    whole = _call(client, {"handle_id": handle, "query": _PATH_AND_FILTER})["error"]
    assert whole["message"].startswith(
        "invalid query spec: 1 validation error(s); first: query: Value error"
    )
    too_long = {"filter": [{"field": "name", "op": "IN", "value": list(range(1001))}]}
    value = _call(client, {"handle_id": handle, "query": too_long})["error"]
    assert value["message"].startswith(
        "invalid query spec: 1 validation error(s); first: query.filter[0].value: "
    )
    assert value["data"] == {"reason": "invalid_query"}


@_as_operator
def test_on_path_that_is_not_in_the_document_names_query_path(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    small_form: None,
) -> None:
    client, op = client_with_operator
    store = MemoryResultStore()
    handle_id = uuid4()
    store.seed_document(
        tenant_id=OPERATOR_TENANT_ID,
        operator_sub=op.sub,
        handle_id=handle_id,
        document={"name": "pod-1"},
    )
    monkeypatch.setattr(result_query_core, "get_result_handle_store", lambda: store)
    args = {"handle_id": str(handle_id), "query": {"path": "/missing"}}
    error = _call(client, args)["error"]
    assert error["code"] == INVALID_PARAMS
    assert error["message"].startswith("query.path: ")


@_as_operator
def test_on_no_match_lists_real_values(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    handle: str,
    small_form: None,
) -> None:
    client, _op = client_with_operator
    payload = _payload(_call(client, {"handle_id": handle, "query": _NO_MATCH}))
    assert payload["rows"] == []
    assert payload["no_match"]["values"] == {"power_state": ["POWERED_ON", "POWERED_OFF"]}
    assert payload["no_match"]["note"].startswith("No row matched the filter.")


@_as_operator
def test_on_contains_filters_in_any_case(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    handle: str,
    small_form: None,
) -> None:
    client, _op = client_with_operator
    query = {"filter": [{"field": "name", "op": "contains", "value": "WEB"}]}
    payload = _payload(_call(client, {"handle_id": handle, "query": query}))
    assert [row["name"] for row in payload["rows"]] == ["web-01", "Web-02"]
    assert "no_match" not in payload


@_as_operator
def test_on_paging_does_not_change(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    handle: str,
    small_form: None,
) -> None:
    client, _op = client_with_operator
    payload = _payload(_call(client, {"handle_id": handle, "offset": 1, "limit": 1}))
    assert payload["rows"] == [_ROWS[1]]
    assert "no_match" not in payload


# ---------------------------------------------------------------------------
# Tool text budget with the switch on
# ---------------------------------------------------------------------------


def test_on_tool_text_stays_inside_the_budget(
    monkeypatch: pytest.MonkeyPatch, small_form: None
) -> None:
    tools = _default_session_tools()
    assert all(d.surface is ToolSurface.WORKING for d in tools)
    result_query = next(d for d in tools if d.name == "result_query")
    assert "contains" in str(result_query.inputSchema)
    assert tool_text_size(result_query) <= MAX_CHARS_PER_TOOL
    assert sum(tool_text_size(d) for d in tools) <= MAX_CHARS_TOTAL

    monkeypatch.setenv("MCP_SHORT_TOOL_TEXTS", "true")
    get_settings.cache_clear()
    short_tools = _default_session_tools()
    short_query = next(d for d in short_tools if d.name == "result_query")
    assert "contains" in str(short_query.inputSchema)
    assert sum(tool_text_size(d) for d in short_tools) <= MAX_CHARS_TOTAL_SHORT
