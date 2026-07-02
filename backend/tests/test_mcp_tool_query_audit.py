# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``query_audit`` MCP meta-tool (G8.1-T4 #468).

Coverage matrix (issue #468 acceptance criteria):

* AC1 — ``query_audit`` registered at MCP server startup; visible in
  ``tools/list``.
* AC2 — Tool description names what + when-to-call + when-NOT-to-call
  (the load-bearing agent UX contract).
* AC3 — ``tools/call query_audit {"target": "rdc-vcenter", "since": "24h"}``
  dispatches through T1 with the right filter shape; ``since`` parses to
  a tz-aware datetime at the tool boundary.
* AC4 — Tenant scope injected from the operator's JWT, not the
  arguments dict.
* AC5 — ``op_class="audit_query"`` is the declarative contract on the
  :class:`ToolDefinition`. End-to-end aggregate-only-broadcast
  verification is T5's job (#469).
* AC6 — ``tools/list`` schema validates as JSON-Schema-2020-12;
  MEHO-internal fields stripped from the wire shape.
* AC7 — (MCP-inspector smoke test) — out of scope here; T5 covers it.
* AC8 — Per-shape conveniences (``audit.show`` / ``audit.who_touched`` /
  ``audit.my_recent``) NOT registered against MCP — only ``query_audit``.
* AC9 — ruff + mypy clean: Phase 7 verification, not a test here.

The substrate (:func:`query_audit`) is patched at the tool's import
site so route tests don't depend on PG/SQLite-seeded audit rows. The
substrate has its own coverage in
``tests/test_audit_query_handler.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from meho_backplane.audit_query import (
    AuditQueryResult,
    InvalidCursorError,
    UnsupportedFilterError,
)
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.registry import get_tool
from meho_backplane.mcp.schemas import INVALID_PARAMS
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_PATCH_TARGET = "meho_backplane.mcp.tools.audit.query_audit"


def _empty_result() -> AuditQueryResult:
    return AuditQueryResult(rows=[], next_cursor=None)


# ---------------------------------------------------------------------------
# AC1 + AC6 — registration + wire shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_list_exposes_query_audit(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC1: ``query_audit`` appears in the MCP tool catalogue."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert response.status_code == 200
    body = response.json()
    names = {t["name"] for t in body["result"]["tools"]}
    assert "query_audit" in names


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_list_input_schema_is_strict_2020_12(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC6: inputSchema is JSON-Schema-2020-12 well-formed; wire fields stripped."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    tools = {t["name"]: t for t in response.json()["result"]["tools"]}
    tool = tools["query_audit"]
    schema = tool["inputSchema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    # Every filter field is optional; no `required` array.
    assert "required" not in schema or schema["required"] == []
    # MEHO-internal fields stripped from the wire shape.
    assert "required_role" not in tool
    assert "op_class" not in tool


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_list_description_contains_when_to_call_guidance(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC2: description names WHAT + WHEN-TO + WHEN-NOT — agent UX contract."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
    )
    tools = {t["name"]: t for t in response.json()["result"]["tools"]}
    desc = tools["query_audit"]["description"].lower()
    assert "audit" in desc
    assert "when to call" in desc
    assert "when not to call" in desc
    # The narrow-waist call-out: the agent should know shortcuts are
    # filter combinations, not separate tools.
    assert "filter combinations" in desc or "filter combination" in desc


def test_registered_definition_has_audit_query_op_class() -> None:
    """AC5: ``op_class="audit_query"`` is the declarative contract.

    The broadcast classifier reads this field via
    :func:`~meho_backplane.broadcast.events.redact_payload` to switch to
    aggregate-only mode for ``audit_query``. End-to-end verification
    that the chain reaches the SSE feed correctly is T5's job (#469).
    """
    entry = get_tool("query_audit")
    assert entry is not None
    defn, _handler = entry
    assert defn.op_class == "audit_query"
    assert defn.required_role == TenantRole.OPERATOR


def test_per_shape_shortcuts_not_registered_against_mcp() -> None:
    """AC8: ``audit.show`` / ``audit.who_touched`` / ``audit.my_recent`` are CLI-only.

    Per CLAUDE.md narrow-waist postulate (#5) the agent sees exactly
    ONE audit tool; the shortcuts collapse into filter combinations on
    ``query_audit``. Anyone registering a per-shape tool would defeat
    the narrow-waist contract.
    """
    for shortcut in ("audit.show", "audit.who_touched", "audit.my_recent"):
        assert get_tool(shortcut) is None, (
            f"unexpected per-shape MCP tool registered: {shortcut!r} — "
            "narrow-waist postulate (CLAUDE.md #5) requires only query_audit"
        )


# ---------------------------------------------------------------------------
# AC3 + AC4 — handler dispatch + tenant scoping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_call_empty_arguments_dispatches_no_filter_query(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Empty arguments → substrate called with all-None filters + ``limit=100``."""
    client, op = client_with_operator
    mock_query = AsyncMock(return_value=_empty_result())
    with patch(_PATCH_TARGET, new=mock_query):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {"name": "query_audit", "arguments": {}},
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["result"]["isError"] is False
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload == {"rows": [], "next_cursor": None}
    mock_query.assert_awaited_once()
    filters = mock_query.await_args.args[0]
    assert mock_query.await_args.kwargs["tenant_id"] == op.tenant_id
    assert filters.target is None
    assert filters.principal is None
    assert filters.since is None
    assert filters.until is None
    assert filters.limit == 100


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_call_since_24h_parses_to_tz_aware_datetime(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC3: ``since="24h"`` resolves to ``now - 24h`` at the tool boundary."""
    client, _op = client_with_operator
    mock_query = AsyncMock(return_value=_empty_result())
    with patch(_PATCH_TARGET, new=mock_query):
        before = datetime.now(UTC)
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 11,
                "method": "tools/call",
                "params": {
                    "name": "query_audit",
                    "arguments": {"target": "rdc-vcenter", "since": "24h"},
                },
            },
        )
        after = datetime.now(UTC)
    assert response.status_code == 200
    filters = mock_query.await_args.args[0]
    assert filters.target == "rdc-vcenter"
    assert filters.since is not None
    assert filters.since.tzinfo is not None
    # The handler subtracts 24h from `now`; the parsed value lands in
    # the [before - 24h, after - 24h] band.
    assert (before - timedelta(hours=24, seconds=1)) <= filters.since
    assert filters.since <= (after - timedelta(hours=24) + timedelta(seconds=1))


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_call_tenant_id_taken_from_operator_jwt(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC4: tenant_id comes from the operator's JWT — never from arguments.

    Because the inputSchema has ``additionalProperties: false``, an agent
    that tries to smuggle ``tenant_id`` in the arguments dict gets a
    -32602 from the dispatcher's jsonschema layer. This test pins both
    sides of the invariant: legitimate calls are scoped to the JWT
    tenant, and the smuggling vector is rejected.
    """
    client, op = client_with_operator
    mock_query = AsyncMock(return_value=_empty_result())
    with patch(_PATCH_TARGET, new=mock_query):
        # Legitimate call — substrate receives JWT tenant_id.
        post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 12,
                "method": "tools/call",
                "params": {"name": "query_audit", "arguments": {}},
            },
        )
    assert mock_query.await_args.kwargs["tenant_id"] == op.tenant_id
    assert op.tenant_id == OPERATOR_TENANT_ID  # fixture sanity

    # Smuggling attempt — `tenant_id` is not a schema field; the
    # dispatcher rejects via additionalProperties: false.
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 13,
            "method": "tools/call",
            "params": {
                "name": "query_audit",
                "arguments": {
                    "tenant_id": "11111111-1111-1111-1111-111111111111",
                },
            },
        },
    )
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_call_handler_returns_result_model_dump(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """The substrate's :class:`AuditQueryResult` round-trips through model_dump."""
    from meho_backplane.audit_query import AuditEntry

    client, _op = client_with_operator
    entry = AuditEntry(
        id=UUID("11111111-1111-1111-1111-111111111111"),
        ts=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
        tenant_id=OPERATOR_TENANT_ID,
        principal_sub="op-1",
        principal_name=None,
        target_id=None,
        target_name=None,
        method="POST",
        path="/api/v1/audit/query",
        status_code=200,
        request_id=None,
        duration_ms=None,
        payload={"op_id": "meho.audit.query", "op_class": "audit_query"},
        op_id="meho.audit.query",
        op_class="audit_query",
        result_status="ok",
        parent_audit_id=None,
        agent_session_id=None,
        work_ref=None,
        policy_decision=None,
        broadcast_event_id=None,
    )
    mock_query = AsyncMock(
        return_value=AuditQueryResult(rows=[entry], next_cursor="next-page-cursor"),
    )
    with patch(_PATCH_TARGET, new=mock_query):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 14,
                "method": "tools/call",
                "params": {"name": "query_audit", "arguments": {}},
            },
        )
    body = response.json()
    assert body["result"]["isError"] is False
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["next_cursor"] == "next-page-cursor"
    assert len(payload["rows"]) == 1
    assert payload["rows"][0]["id"] == "11111111-1111-1111-1111-111111111111"
    assert payload["rows"][0]["op_id"] == "meho.audit.query"


# ---------------------------------------------------------------------------
# Error mapping (DurationParseError / InvalidCursorError / UnsupportedFilterError)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_call_bad_duration_returns_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A garbage ``since`` value surfaces as ``-32602`` with the parser message."""
    client, _op = client_with_operator
    mock_query = AsyncMock(return_value=_empty_result())
    with patch(_PATCH_TARGET, new=mock_query):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 20,
                "method": "tools/call",
                "params": {
                    "name": "query_audit",
                    "arguments": {"since": "twentyfour-hours"},
                },
            },
        )
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "duration" in body["error"]["message"].lower()
    mock_query.assert_not_awaited()


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_call_invalid_cursor_returns_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Substrate-raised :class:`InvalidCursorError` maps to ``-32602``."""
    client, _op = client_with_operator
    mock_query = AsyncMock(side_effect=InvalidCursorError("cursor is not valid base64"))
    with patch(_PATCH_TARGET, new=mock_query):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 21,
                "method": "tools/call",
                "params": {
                    "name": "query_audit",
                    "arguments": {"cursor": "not-base64"},
                },
            },
        )
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "cursor" in body["error"]["message"].lower()


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_call_unsupported_filter_returns_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Substrate-raised :class:`UnsupportedFilterError` maps to ``-32602``."""
    client, _op = client_with_operator
    mock_query = AsyncMock(
        side_effect=UnsupportedFilterError(
            "parent_audit_id filter not supported in v0.2 — column lands with G0.6-T7 (#398)",
        ),
    )
    with patch(_PATCH_TARGET, new=mock_query):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 22,
                "method": "tools/call",
                "params": {
                    "name": "query_audit",
                    "arguments": {
                        "parent_audit_id": "11111111-1111-1111-1111-111111111111",
                    },
                },
            },
        )
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "parent_audit_id" in body["error"]["message"]


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_call_rejects_additional_properties(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``additionalProperties: false`` blocks unknown fields at the schema layer."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 23,
            "method": "tools/call",
            "params": {
                "name": "query_audit",
                "arguments": {"unknown_field": "should be rejected"},
            },
        },
    )
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.READ_ONLY],
    indirect=True,
)
def test_tools_call_read_only_role_returns_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``read_only`` is below the operator gate — call is forbidden.

    The RBAC list-time filter hides the tool from ``tools/list`` for
    sub-operator roles; the per-call re-check in ``handle_tools_call``
    surfaces a forbidden tool as ``INVALID_PARAMS`` (the JSON-RPC spec
    lacks an HTTP-403 analogue, and the transport already 401s at the
    JWT layer).
    """
    client, _op = client_with_operator
    mock_query = AsyncMock(return_value=_empty_result())
    with patch(_PATCH_TARGET, new=mock_query):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 30,
                "method": "tools/call",
                "params": {"name": "query_audit", "arguments": {}},
            },
        )
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "forbidden" in body["error"]["message"].lower()
    mock_query.assert_not_awaited()


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_tools_call_tenant_admin_role_passes_gate(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``tenant_admin`` clears the operator gate — same tenant scope as operator."""
    client, _op = client_with_operator
    mock_query = AsyncMock(return_value=_empty_result())
    with patch(_PATCH_TARGET, new=mock_query):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 31,
                "method": "tools/call",
                "params": {"name": "query_audit", "arguments": {}},
            },
        )
    body = response.json()
    assert body["result"]["isError"] is False
    mock_query.assert_awaited_once()
