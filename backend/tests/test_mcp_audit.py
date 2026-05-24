# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the MCP-specific audit integration (G0.5-T5, #250).

Covers every acceptance criterion on issue #250:

* ``tools/call meho.status`` writes exactly one ``audit_log`` row with
  method=MCP, path=/mcp/tools/call/meho.status, status_code=200,
  payload carries the empty-args params_hash + op_class=read.
* ``resources/read meho://tenant/<id>/info`` writes exactly one
  ``audit_log`` row with path=/mcp/resources/read/<uri>.
* ``tools/call`` against an unknown tool writes a row with status_code=404
  and op_class=unknown.
* ``compute_params_hash`` is deterministic across dict insertion order
  and equivalent representations.
* The chassis :class:`~meho_backplane.audit.AuditMiddleware` skips
  ``/mcp`` requests entirely (no duplicate row per JSON-RPC envelope).
* Audit-write failure converts an otherwise-successful MCP call into a
  JSON-RPC ``-32603`` Internal Error (fail-closed contract).
* Every row carries the correct ``tenant_id`` populated from the
  validated :class:`Operator`.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.mcp.audit import compute_params_hash, write_mcp_audit_row
from meho_backplane.mcp.schemas import INTERNAL_ERROR, INVALID_PARAMS, INVALID_REQUEST
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import (
    build_operator,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
    seeded_operator_tenant,  # noqa: F401 — pytest-discovered fixture
)


async def _audit_rows() -> list[AuditLog]:
    """Read every ``audit_log`` row, ordered by ``occurred_at``."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).order_by(AuditLog.occurred_at),
        )
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# compute_params_hash — pure-function determinism
# ---------------------------------------------------------------------------


def test_params_hash_empty_dict_is_stable() -> None:
    """``compute_params_hash({})`` returns the canonical empty-object hash.

    Regression-locks the hash so a future change to the canonicalisation
    rule (separators, sort_keys) would surface here loudly instead of
    silently invalidating every existing payload row's params_hash.
    """
    expected = (
        # SHA-256("{}") — the canonical JSON encoding of an empty dict
        # under ``sort_keys=True, separators=(",", ":")``.
        "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
    )
    assert compute_params_hash({}) == expected


def test_params_hash_is_order_independent() -> None:
    """Same dict-keys in different insertion orders → same hash."""
    a = {"foo": 1, "bar": 2}
    b = {"bar": 2, "foo": 1}
    assert compute_params_hash(a) == compute_params_hash(b)


def test_params_hash_differs_for_distinct_inputs() -> None:
    """``{x: 1}`` and ``{x: 2}`` produce different digests."""
    assert compute_params_hash({"x": 1}) != compute_params_hash({"x": 2})


# ---------------------------------------------------------------------------
# tools/call audit — happy path + status_code derivation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_call_meho_status_writes_one_mcp_audit_row(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """AC #1: tools/call meho.status writes one ``method="MCP"`` row.

    Post-G0.6-T-Refactor-Vault, the ``meho.status`` handler reaches
    :func:`~meho_backplane.api.v1.health.build_health_response` which
    dispatches ``vault.kv.read`` through the G0.6 dispatcher; the
    dispatcher writes its own ``method="DISPATCH"`` audit row for the
    typed-op call. That row is a separate granularity (per-operation
    inside the MCP envelope) and does **not** invalidate AC #1 — the
    invariant the test pins is the **MCP-envelope** row that the
    chassis writes, not the dispatcher's internal accounting.

    Concretely: filter the audit table on ``method="MCP"`` to assert
    the single envelope row; the dispatcher row is verified
    independently in :mod:`tests.test_operations_dispatcher`.
    """
    client, op = client_with_operator

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "meho.status", "arguments": {}},
        },
    )
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False

    rows = await _audit_rows()
    mcp_rows = [r for r in rows if r.method == "MCP"]
    assert len(mcp_rows) == 1
    row = mcp_rows[0]
    assert row.operator_sub == op.sub
    assert row.tenant_id == op.tenant_id
    assert row.method == "MCP"
    assert row.path == "/mcp/tools/call/meho.status"
    assert row.status_code == 200
    assert row.payload["op_id"] == "meho.status"
    assert row.payload["op_class"] == "read"
    assert row.payload["params_hash"] == compute_params_hash({})


@pytest.mark.asyncio
async def test_tools_call_unknown_tool_writes_audit_row_with_404(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """AC #3: unknown.tool → row with status_code=404, op_class=unknown."""
    client, _op = client_with_operator

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "no.such.tool", "arguments": {}},
        },
    )
    assert response.json()["error"]["code"] == INVALID_PARAMS

    rows = await _audit_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.path == "/mcp/tools/call/no.such.tool"
    assert row.status_code == 404
    assert row.payload["op_class"] == "unknown"


# ---------------------------------------------------------------------------
# resources/read audit — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resources_read_tenant_info_writes_one_audit_row(
    client_with_operator: tuple[TestClient, Operator],
    seeded_operator_tenant: None,
) -> None:
    """AC #2: resources/read meho://tenant/<id>/info → one row, path matches."""
    client, op = client_with_operator
    uri = f"meho://tenant/{op.tenant_id}/info"

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "resources/read",
            "params": {"uri": uri},
        },
    )
    assert response.status_code == 200
    assert "result" in response.json()

    rows = await _audit_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.operator_sub == op.sub
    assert row.tenant_id == op.tenant_id
    assert row.method == "MCP"
    assert row.path == f"/mcp/resources/read/{uri}"
    assert row.status_code == 200
    assert row.payload["uri"] == uri
    assert row.payload["op_class"] == "read"


# ---------------------------------------------------------------------------
# AC #7: chassis AuditMiddleware does NOT double-audit /mcp requests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_envelope_does_not_produce_chassis_audit_row(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """The chassis ``AuditMiddleware`` skips ``/mcp`` so the JSON-RPC POST
    contributes zero rows; the MCP handler writes the single envelope row.

    Without the path-prefix exclusion in :class:`AuditMiddleware`, the
    middleware would write a row per JSON-RPC POST (wrong granularity)
    AND each MCP handler would write its own row (correct granularity).
    Post-G0.6-T-Refactor-Vault, the ``meho.status`` handler also routes
    through the G0.6 dispatcher (which writes a per-operation
    ``method="DISPATCH"`` row); the contract this test pins is that
    no *chassis-middleware* row appears for the JSON-RPC POST itself —
    so we filter on ``method="MCP"`` to isolate the envelope-level row.
    """
    client, _op = client_with_operator

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "meho.status", "arguments": {}},
        },
    )
    assert response.status_code == 200

    rows = await _audit_rows()
    # Exactly one ``method="MCP"`` row from the MCP handler; the chassis
    # AuditMiddleware path-prefix exclusion
    # (audit.py:_AUDIT_SKIP_PATH_PREFIXES) keeps the JSON-RPC envelope
    # from adding an HTTP-method row. The G0.6 dispatcher additionally
    # contributes a ``method="DISPATCH"`` row for the inner
    # ``vault.kv.read`` call; that row is intentional and verified
    # separately in :mod:`tests.test_operations_dispatcher`.
    mcp_rows = [r for r in rows if r.method == "MCP"]
    assert len(mcp_rows) == 1
    assert mcp_rows[0].method == "MCP"
    http_rows = [r for r in rows if r.method in ("GET", "POST", "PUT", "DELETE", "PATCH")]
    assert http_rows == [], "chassis AuditMiddleware should skip /mcp paths; found HTTP-method rows"


# ---------------------------------------------------------------------------
# AC #5: audit-write failure → fail-closed -32603
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_write_failure_converts_call_to_internal_error(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """When :func:`write_mcp_audit_row` raises, the call fails with -32603.

    Fail-closed contract: an audit-write failure invalidates the
    operation. The client sees -32603 INTERNAL_ERROR; no
    ``result`` envelope is returned even if the handler itself
    succeeded.
    """
    client, _op = client_with_operator

    async def _explode(**_kwargs: Any) -> None:
        raise RuntimeError("simulated audit-write failure")

    with patch(
        "meho_backplane.mcp.handlers.write_mcp_audit_row",
        side_effect=_explode,
    ):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "meho.status", "arguments": {}},
            },
        )

    body = response.json()
    assert body["error"]["code"] == INTERNAL_ERROR


# ---------------------------------------------------------------------------
# write_mcp_audit_row helper-level test (independent of MCP dispatch)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_mcp_audit_row_persists_every_field() -> None:
    """The helper round-trips every field including the JSON ``payload``."""
    op = build_operator()
    request_id = uuid4()
    payload = {"op_id": "test.tool", "params_hash": "abc123", "op_class": "read"}

    await write_mcp_audit_row(
        operator=op,
        method="MCP",
        path="/mcp/tools/call/test.tool",
        status_code=200,
        duration_ms=12.34,
        payload=payload,
        request_id=request_id,
    )

    rows = await _audit_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.operator_sub == op.sub
    assert row.tenant_id == op.tenant_id
    assert row.method == "MCP"
    assert row.path == "/mcp/tools/call/test.tool"
    assert row.status_code == 200
    assert row.request_id == request_id
    # SQLite stores Numeric via _PORTABLE_JSON if applicable; check value
    assert float(row.duration_ms or 0) == pytest.approx(12.34)
    assert json.loads(json.dumps(row.payload)) == payload


# ---------------------------------------------------------------------------
# G8.2-T2 (#1010): Mcp-Session-Id capture → agent_session_id column
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_header_propagates_to_agent_session_id(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """AC: a ``Mcp-Session-Id`` header lands on ``audit_log.agent_session_id``."""
    client, _op = client_with_operator
    session_id = uuid4()

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "meho.status", "arguments": {}},
        },
        headers={"Mcp-Session-Id": str(session_id)},
    )
    assert response.status_code == 200

    rows = await _audit_rows()
    mcp_rows = [r for r in rows if r.method == "MCP"]
    assert len(mcp_rows) == 1
    assert mcp_rows[0].agent_session_id == session_id


@pytest.mark.asyncio
async def test_absent_session_header_gets_fresh_uuid_per_call(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """AC: no header → non-NULL ``agent_session_id``; two calls differ.

    Single-call sessions are valid per the MCP spec; the fresh uuid4
    keeps the column non-NULL so the row is still session-addressable.
    Two header-less calls must get two distinct ids — otherwise the
    fallback would collide every unattributed call into one bucket.
    """
    client, _op = client_with_operator
    envelope = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "meho.status", "arguments": {}},
    }

    assert post_mcp(client, envelope).status_code == 200
    assert post_mcp(client, {**envelope, "id": 2}).status_code == 200

    rows = await _audit_rows()
    mcp_rows = [r for r in rows if r.method == "MCP"]
    assert len(mcp_rows) == 2
    ids = [r.agent_session_id for r in mcp_rows]
    assert all(i is not None for i in ids)
    assert ids[0] != ids[1]


@pytest.mark.asyncio
async def test_malformed_session_header_falls_back_to_fresh_uuid(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """AC: a non-UUID ``Mcp-Session-Id`` does not 500 — it falls back.

    A malformed *client* header can't go in the ``uuid`` column, so it
    is treated as absent: the call succeeds (no transport error) and the
    row carries a fresh, non-NULL uuid4 rather than the bad header value.
    """
    client, _op = client_with_operator

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "meho.status", "arguments": {}},
        },
        headers={"Mcp-Session-Id": "not-a-uuid"},
    )
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False

    rows = await _audit_rows()
    mcp_rows = [r for r in rows if r.method == "MCP"]
    assert len(mcp_rows) == 1
    assert mcp_rows[0].agent_session_id is not None


@pytest.mark.asyncio
async def test_require_session_id_rejects_missing_header_without_audit_row(
    client_with_operator: tuple[TestClient, Operator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC: ``MCP_REQUIRE_SESSION_ID=true`` + no header → -32600, no row.

    The reject must fire *before* dispatch, so no audit row is written
    for the rejected call (the operation never ran).
    """
    client, _op = client_with_operator
    monkeypatch.setenv("MCP_REQUIRE_SESSION_ID", "true")
    get_settings.cache_clear()

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "meho.status", "arguments": {}},
        },
    )
    assert response.json()["error"]["code"] == INVALID_REQUEST

    rows = await _audit_rows()
    assert rows == []


@pytest.mark.asyncio
async def test_require_session_id_accepts_when_header_present(
    client_with_operator: tuple[TestClient, Operator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC: require-mode still succeeds when the header is supplied."""
    client, _op = client_with_operator
    monkeypatch.setenv("MCP_REQUIRE_SESSION_ID", "true")
    get_settings.cache_clear()
    session_id = uuid4()

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "meho.status", "arguments": {}},
        },
        headers={"Mcp-Session-Id": str(session_id)},
    )
    assert response.status_code == 200

    rows = await _audit_rows()
    mcp_rows = [r for r in rows if r.method == "MCP"]
    assert len(mcp_rows) == 1
    assert mcp_rows[0].agent_session_id == session_id


@pytest.mark.asyncio
async def test_write_helper_leaves_agent_session_id_null_without_contextvar() -> None:
    """HTTP-shaped rows stay NULL: the writer reads no session contextvar.

    The chassis ``AuditMiddleware`` never binds ``mcp_session_id``, so a
    direct ``write_mcp_audit_row`` call with no contextvar bound (the
    helper-level path) leaves ``agent_session_id`` / ``parent_audit_id``
    NULL — proving the column is opt-in via the MCP transport's bind,
    not a default on every row.
    """
    import structlog

    structlog.contextvars.clear_contextvars()
    op = build_operator()

    await write_mcp_audit_row(
        operator=op,
        method="MCP",
        path="/mcp/tools/call/test.tool",
        status_code=200,
        duration_ms=1.0,
        payload={"op_id": "test.tool", "op_class": "read"},
    )

    rows = await _audit_rows()
    assert len(rows) == 1
    assert rows[0].agent_session_id is None
    assert rows[0].parent_audit_id is None
