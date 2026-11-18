# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""One session asks for each result once, with ``?results=structured`` (#4716).

These tests check:

* reading the ``results`` URL value: blank means the default,
  ``structured`` asks for structured-only results, any other value is
  refused;
* a default session gets the full result as compact JSON text plus
  ``structuredContent``, as before;
* a structured-only session gets one short text line plus
  ``structuredContent`` for a tool with an ``outputSchema``;
* a tool without an ``outputSchema`` and an error result keep the full
  text in a structured-only session;
* the real size count matches what is sent in each mode;
* the delivery ceilings count the text as spaced JSON, so they decide
  exactly as before the text became compact (the same hits fit, the same
  results are ``partial``), and a Memory read is measured exactly;
* over HTTP, the URL parameter changes only the session that sent it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.mcp.handlers import handle_tools_call
from meho_backplane.mcp.registry import get_tool
from meho_backplane.mcp.resources import memory as memory_resource
from meho_backplane.mcp.schemas import INVALID_REQUEST
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.mcp.structured_results import (
    UnknownToolResultsError,
    parse_tool_results,
)
from meho_backplane.mcp.tools import _memory_evidence
from meho_backplane.operations.delivery_budget import (
    STRUCTURED_ONLY_TEXT,
    bound_complete_response,
    measure_call_operation_bytes,
    measure_delivered_wire_bytes,
    sent_wire_bytes,
    tool_result_text,
)
from meho_backplane.untrusted_text import with_data_notice
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_RESULT: dict[str, Any] = {"status": "ok", "op_id": "<op-id>", "result": {"rows": [1, 2]}}
_CALL_ARGS: dict[str, Any] = {"connector_id": "<connector-id>", "op_id": "<op-id>"}


def _operator(*, structured: bool) -> Operator:
    return Operator(
        sub="op-structured-results",
        raw_jwt="fixture-jwt-not-real",
        tenant_id=OPERATOR_TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
        mcp_structured_results=structured,
    )


# ---------------------------------------------------------------------------
# Reading the URL value
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("values", [[], [""], ["  "]])
def test_no_or_blank_value_means_the_default(values: list[str]) -> None:
    assert parse_tool_results(values) is False


@pytest.mark.parametrize("values", [["structured"], ["STRUCTURED"], [" Structured "]])
def test_structured_is_read(values: list[str]) -> None:
    assert parse_tool_results(values) is True


@pytest.mark.parametrize("values", [["text"], ["both"], ["structured", "text"]])
def test_other_values_are_refused(values: list[str]) -> None:
    with pytest.raises(UnknownToolResultsError) as exc_info:
        parse_tool_results(values)
    assert "results=structured" in str(exc_info.value)


def test_error_text_cuts_a_long_value() -> None:
    with pytest.raises(UnknownToolResultsError) as exc_info:
        parse_tool_results(["x" * 500])
    assert "x" * 65 not in str(exc_info.value)


# ---------------------------------------------------------------------------
# The text block in each mode
# ---------------------------------------------------------------------------


def test_text_is_compact_json() -> None:
    text = tool_result_text(_RESULT)
    assert ", " not in text and ": " not in text
    assert json.loads(text) == _RESULT


async def _call(tool: str, result: dict[str, Any], *, structured: bool) -> dict[str, Any]:
    entry = get_tool(tool)
    assert entry is not None
    handler = AsyncMock(return_value=result)
    arguments = _CALL_ARGS if tool == "call_operation" else {}
    with (
        patch("meho_backplane.mcp.handlers.get_tool", return_value=(entry[0], handler)),
        patch("meho_backplane.mcp.handlers.write_mcp_audit_row", new=AsyncMock()),
    ):
        return await handle_tools_call(
            _operator(structured=structured), {"name": tool, "arguments": arguments}
        )


async def test_default_session_gets_the_full_text_and_the_object() -> None:
    response = await _call("call_operation", _RESULT, structured=False)
    assert response["content"] == [{"type": "text", "text": tool_result_text(_RESULT)}]
    assert response["structuredContent"] == _RESULT


async def test_structured_session_gets_the_result_once() -> None:
    response = await _call("call_operation", _RESULT, structured=True)
    assert response["content"] == [{"type": "text", "text": STRUCTURED_ONLY_TEXT}]
    assert response["structuredContent"] == _RESULT
    assert response["isError"] is False


async def test_structured_session_keeps_the_text_of_an_error() -> None:
    failed = {"status": "error", "op_id": "<op-id>", "extras": {"error_code": "bad_params"}}
    response = await _call("call_operation", failed, structured=True)
    assert response["isError"] is True
    assert json.loads(response["content"][0]["text"]) == failed
    assert response["structuredContent"] == failed


async def test_structured_session_keeps_the_text_of_a_tool_without_schema() -> None:
    entry = get_tool("meho_status")
    assert entry is not None and entry[0].outputSchema is None
    status = {"status": "ok"}
    response = await _call("meho_status", status, structured=True)
    assert json.loads(response["content"][0]["text"]) == status
    assert "structuredContent" not in response


# ---------------------------------------------------------------------------
# The real size count matches what is sent
# ---------------------------------------------------------------------------


def _body_len(value: Any) -> int:
    """Length of *value* as Starlette's JSONResponse writes it."""
    return len(JSONResponse(content=value).body)


@pytest.mark.parametrize("structured_only", [False, True])
async def test_sent_count_matches_what_is_sent(structured_only: bool) -> None:
    response = await _call("call_operation", _RESULT, structured=structured_only)
    sent = _body_len(response["content"][0]["text"]) + _body_len(response["structuredContent"])
    assert sent_wire_bytes(_RESULT, structured_only=structured_only) == sent


def test_structured_only_needs_a_structured_copy() -> None:
    """A tool without ``structuredContent`` always sends the full text."""
    full = sent_wire_bytes(_RESULT, structured=False)
    assert sent_wire_bytes(_RESULT, structured=False, structured_only=True) == full


def test_structured_only_sends_fewer_bytes() -> None:
    both = sent_wire_bytes(_RESULT)
    once = sent_wire_bytes(_RESULT, structured_only=True)
    assert once == _body_len(STRUCTURED_ONLY_TEXT) + _body_len(_RESULT)
    assert once < both


# ---------------------------------------------------------------------------
# The ceilings decide as before the text became compact
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("structured", [True, False])
def test_ceiling_count_uses_the_spaced_text(structured: bool) -> None:
    """The count is ``json.dumps(result)`` with spaces, as before the compact text."""
    expected = _body_len(json.dumps(_RESULT))
    if structured:
        expected += _body_len(_RESULT)
    assert measure_delivered_wire_bytes(_RESULT, structured=structured) == expected


@pytest.mark.parametrize("structured_only", [False, True])
def test_ceiling_count_is_never_smaller_than_what_is_sent(structured_only: bool) -> None:
    assert measure_delivered_wire_bytes(_RESULT) > sent_wire_bytes(
        _RESULT, structured_only=structured_only
    )


def _wide_result() -> OperationResult:
    rows = [{"name": f"row-{i}", "size": i, "tags": ["a", "b"]} for i in range(60)]
    return OperationResult(status="ok", op_id="<op-id>", duration_ms=1.0, result={"rows": rows})


def test_completed_call_boundary_is_unchanged() -> None:
    """A limit between the compact and the spaced size still trims the result."""
    result = _wide_result()
    counted = measure_call_operation_bytes(result)
    compact = sent_wire_bytes(with_data_notice(result.model_dump(mode="json")))
    assert compact < counted
    assert bound_complete_response(result, counted) is result
    trimmed = bound_complete_response(result, compact)
    assert trimmed.delivery == "unavailable"
    assert trimmed.result is None


def _hit(i: int) -> dict[str, Any]:
    return {"slug": f"note-{i}", "snippet": "x, y: z, " * 20, "score": 0.5}


def test_memory_search_boundary_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """A limit between the compact and the spaced size keeps one hit, as before."""
    hits = [_hit(1), _hit(2)]
    both = _memory_evidence._search_response(hits, 0)
    counted = measure_delivered_wire_bytes(both)
    assert sent_wire_bytes(both) < counted
    limit = SimpleNamespace(result_complete_response_wire_max_bytes=counted - 1)
    monkeypatch.setattr(_memory_evidence, "get_settings", lambda: limit)
    response = _memory_evidence.bounded_search_response(hits)
    assert response["hits"] == [hits[0]]
    assert response["delivery"] == "partial"
    assert response["omitted_hits"] == 1


def test_memory_read_limit_counts_the_text_that_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``resources/read`` sends ``json.dumps(body)``; the check counts that exact text."""
    data = {"locator": "<locator>", "body": "x, y: z, " * 40}
    sent = _body_len(json.dumps(data))
    memory = memory_resource
    monkeypatch.setattr(
        memory, "get_settings", lambda: SimpleNamespace(result_query_response_wire_max_bytes=sent)
    )
    memory._check_read_wire_limit(data)
    monkeypatch.setattr(
        memory,
        "get_settings",
        lambda: SimpleNamespace(result_query_response_wire_max_bytes=sent - 1),
    )
    with pytest.raises(McpInvalidParamsError):
        memory._check_read_wire_limit(data)


# ---------------------------------------------------------------------------
# Over HTTP: the ``results`` URL parameter
# ---------------------------------------------------------------------------


def _create_node(client: TestClient, url: str, name: str) -> dict[str, Any]:
    response = client.post(
        url,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "meho_topology_create_node",
                "arguments": {"kind": "vault-role", "name": name},
            },
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "error" not in body, body
    result: dict[str, Any] = body["result"]
    return result


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_http_results_parameter(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, _op = client_with_operator
    once = _create_node(client, "/mcp?results=structured", "structured-results-once")
    assert once["content"] == [{"type": "text", "text": STRUCTURED_ONLY_TEXT}]
    assert once["structuredContent"]["status"]
    # A later request without the parameter gets both copies again.
    both = _create_node(client, "/mcp", "structured-results-both")
    assert json.loads(both["content"][0]["text"]) == both["structuredContent"]


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.parametrize("query", ["results=text", "results=structured&results=both"])
def test_http_unknown_results_is_a_bad_request(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    query: str,
) -> None:
    client, _op = client_with_operator
    response = client.post(f"/mcp?{query}", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == INVALID_REQUEST
    assert "unknown tool results" in error["message"]
