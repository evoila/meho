# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``result_query`` JSONFlux handle read-back MCP tool (G0.20-T7 #1507).

Covers the #1507 acceptance criterion that a ``>50``-row result is fully
retrievable over MCP:

* ``tools/list`` exposes ``result_query`` with a well-formed input schema;
* ``tools/call`` returns the requested window of a spilled handle, scoped
  to the operator's tenant + ``sub`` (never the arguments);
* a ``>50``-row spilled set is fully retrievable by paging;
* an unknown / expired handle is a recoverable ``-32602`` with
  ``data.reason=handle_not_found``;
* an extra argument violates ``additionalProperties: false``.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

import meho_backplane.mcp.tools.result_query as result_query_module
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.result_handle_store import SpilledWindow
from meho_backplane.mcp.schemas import INVALID_PARAMS
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

# Exercise the OPERATOR-role client: result_query requires OPERATOR.
pytestmark = pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)


class _FakeStore:
    """In-memory store the handler reads windows out of.

    Keyed by ``(tenant_id, operator_sub, handle_id)`` so the test can
    assert the handler threads the operator identity from the JWT, not
    the arguments.
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str], list[dict[str, Any]]] = {}

    def seed(
        self,
        *,
        tenant_id: UUID,
        operator_sub: str,
        handle_id: UUID,
        rows: list[dict[str, Any]],
    ) -> None:
        self._rows[(str(tenant_id), operator_sub, str(handle_id))] = rows

    async def fetch_window(
        self,
        *,
        tenant_id: UUID,
        operator_sub: str,
        handle_id: UUID,
        offset: int,
        limit: int,
    ) -> SpilledWindow | None:
        rows = self._rows.get((str(tenant_id), operator_sub, str(handle_id)))
        if rows is None:
            return None
        window = rows[offset : offset + limit] if limit > 0 else []
        return SpilledWindow(
            rows=window,
            total_rows=len(rows),
            stored_rows=len(rows),
            truncated=False,
        )


@pytest.fixture
def fake_store(monkeypatch: pytest.MonkeyPatch) -> _FakeStore:
    """Replace the production store getter with an in-memory fake."""
    store = _FakeStore()
    monkeypatch.setattr(result_query_module, "get_result_handle_store", lambda: store)
    return store


def _call(client: TestClient, args: dict[str, Any]) -> Any:
    return post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "result_query", "arguments": args},
        },
    )


def _structured(result: dict[str, Any]) -> dict[str, Any]:
    """Read a tool result's payload (structuredContent or the text JSON)."""
    structured = result.get("structuredContent")
    if structured is not None:
        return structured
    return json.loads(result["content"][0]["text"])


def test_tools_list_exposes_result_query(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, _op = client_with_operator
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 200
    tools = response.json()["result"]["tools"]
    hits = [t for t in tools if t["name"] == "result_query"]
    assert len(hits) == 1
    schema = hits[0]["inputSchema"]
    assert schema["required"] == ["handle_id"]
    assert schema["additionalProperties"] is False


def test_result_query_returns_window_beyond_inline_sample(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    fake_store: _FakeStore,
) -> None:
    """A >50-row spilled set is fully retrievable over MCP by paging."""
    client, op = client_with_operator
    handle = uuid4()
    rows = [{"i": i, "name": f"row-{i}"} for i in range(60)]
    fake_store.seed(
        tenant_id=OPERATOR_TENANT_ID,
        operator_sub=op.sub,
        handle_id=handle,
        rows=rows,
    )

    # Rows 5..54 (past the 5-row inline sample) are reachable.
    response = _call(client, {"handle_id": str(handle), "offset": 5, "limit": 50})
    assert response.status_code == 200
    body = response.json()
    assert "error" not in body, body
    payload = _structured(body["result"])
    assert payload["total_rows"] == 60
    assert payload["returned_rows"] == 50
    assert payload["truncated"] is False
    assert [r["i"] for r in payload["rows"]] == list(range(5, 55))

    # The tail (rows 55..59) is reachable too — the full set, not just a sample.
    tail = _call(client, {"handle_id": str(handle), "offset": 55, "limit": 50})
    tail_payload = _structured(tail.json()["result"])
    assert [r["i"] for r in tail_payload["rows"]] == list(range(55, 60))


def test_result_query_unknown_handle_is_recoverable_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    fake_store: _FakeStore,
) -> None:
    client, _op = client_with_operator
    response = _call(client, {"handle_id": str(uuid4()), "offset": 0, "limit": 10})
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert body["error"]["data"]["reason"] == "handle_not_found"


def test_result_query_cross_operator_is_a_miss(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    fake_store: _FakeStore,
) -> None:
    """A handle spilled by a different operator surfaces as not-found."""
    client, _op = client_with_operator
    handle = uuid4()
    fake_store.seed(
        tenant_id=OPERATOR_TENANT_ID,
        operator_sub="some-other-operator",
        handle_id=handle,
        rows=[{"i": i} for i in range(60)],
    )
    response = _call(client, {"handle_id": str(handle), "offset": 0, "limit": 10})
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert body["error"]["data"]["reason"] == "handle_not_found"


def test_result_query_invalid_uuid_is_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    fake_store: _FakeStore,
) -> None:
    client, _op = client_with_operator
    response = _call(client, {"handle_id": "not-a-uuid"})
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert body["error"]["data"]["reason"] == "invalid_handle_id"


def test_result_query_rejects_extra_argument(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    fake_store: _FakeStore,
) -> None:
    client, _op = client_with_operator
    response = _call(client, {"handle_id": str(uuid4()), "bogus": 1})
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
