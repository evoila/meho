# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the audit-replay MCP surface (G8.2-T6 #1014).

Two surfaces, one substrate (T3 :func:`replay_session`):

* ``meho_audit_replay`` — a ``tenant_admin`` cross-session forensic
  replay meta-tool. RBAC gate, tenant scoping, count-first 10k guard,
  and tree shape.
* ``query_audit`` with ``shape="tree"`` — an ``operator`` self-session
  replay path. The self-session check rejects any ``agent_session_id``
  that is not the caller's own bound MCP session id (-32602).

The substrate (:func:`replay_session`) and the row-count helper are
patched at the tool's import site so these tests don't depend on a
PG/SQLite-seeded audit graph — that integration coverage is
``tests/test_audit_replay.py`` (substrate) and T7 (E2E acceptance).
The patch points are the names bound in
:mod:`meho_backplane.mcp.tools.audit`.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.audit_query import ReplayNode
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.mcp.registry import get_tool
from meho_backplane.mcp.schemas import INVALID_PARAMS
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_REPLAY_PATCH = "meho_backplane.mcp.tools.audit.replay_session"
_COUNT_PATCH = "meho_backplane.mcp.tools.audit._count_session_rows"

_SESSION_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_OTHER_SESSION_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

#: A second tenant, used to prove the excluded-null tally is tenant-scoped.
_OTHER_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b0")


def _add_audit_row(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    second: int,
    method: str,
    agent_session_id: uuid.UUID | None,
) -> None:
    """Stage one :class:`AuditLog` row at a fixed base + ``second`` offset.

    Mirrors ``tests/test_api_audit_routes.py::_seed_audit_row`` so the MCP
    excluded-null count is exercised against real rows (the substrate
    ``replay_session`` / ``_count_session_rows`` stay patched, but the new
    ``_count_excluded_null_session_rows`` runs its real query here).
    ``session.add`` is synchronous; the caller commits inside its own
    ``session.begin()`` block.
    """
    base = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    session.add(
        AuditLog(
            id=uuid.uuid4(),
            occurred_at=base + timedelta(seconds=second),
            operator_sub="op-1",
            tenant_id=tenant_id,
            method=method,
            path="/mcp",
            status_code=200,
            duration_ms=Decimal("1.0"),
            payload={"op_id": "vsphere.vm.list", "op_class": "read"},
            agent_session_id=agent_session_id,
        ),
    )


def _node(node_id: str, *, depth: int, children: list[ReplayNode] | None = None) -> ReplayNode:
    """Build a minimal :class:`ReplayNode` for the patched substrate return."""
    return ReplayNode(
        id=uuid.UUID(node_id),
        ts=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
        tenant_id=OPERATOR_TENANT_ID,
        principal_sub="op-test",
        principal_name=None,
        target_id=None,
        target_name=None,
        method="MCP",
        path="/mcp/tools/call/call_operation",
        status_code=200,
        request_id=None,
        duration_ms=None,
        payload={"op_id": "vsphere.vm.list"},
        op_id="vsphere.vm.list",
        op_class="read",
        result_status="ok",
        parent_audit_id=None,
        agent_session_id=uuid.UUID(_SESSION_ID),
        work_ref=None,
        policy_decision=None,
        broadcast_event_id=None,
        depth=depth,
        children=children or [],
    )


def _two_level_tree() -> list[ReplayNode]:
    """A root with one child — a minimal multi-level replay forest."""
    child = _node("22222222-2222-2222-2222-222222222222", depth=1)
    root = _node("11111111-1111-1111-1111-111111111111", depth=0, children=[child])
    return [root]


# ---------------------------------------------------------------------------
# meho_audit_replay — registration + RBAC
# ---------------------------------------------------------------------------


def test_replay_tool_registered_as_tenant_admin_audit_query() -> None:
    """The admin tool is registered with the right role + op_class contract.

    ``op_class="audit_query"`` is load-bearing twice over: the registry
    contract, and (via the matching ``meho.audit.`` arm in
    :func:`classify_op`) the MCP broadcast path's aggregate-only redaction.
    """
    entry = get_tool("meho_audit_replay")
    assert entry is not None
    defn, _handler = entry
    assert defn.op_class == "audit_query"
    assert defn.required_role == TenantRole.TENANT_ADMIN


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_replay_tool_in_tools_list_for_admin(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``tenant_admin`` sees ``meho_audit_replay`` in the catalogue."""
    client, _op = client_with_operator
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert "meho_audit_replay" in names


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_replay_tool_hidden_from_operator_tools_list(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """The RBAC list-time filter hides the admin tool from sub-admin roles."""
    client, _op = client_with_operator
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert "meho_audit_replay" not in names


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR, TenantRole.READ_ONLY],
    indirect=True,
)
def test_replay_tool_forbidden_for_non_admin(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``operator`` / ``read_only`` calling the admin tool get -32602 forbidden."""
    client, _op = client_with_operator
    mock_replay = AsyncMock(return_value=[])
    with patch(_REPLAY_PATCH, new=mock_replay):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "meho_audit_replay",
                    "arguments": {"session_id": _SESSION_ID},
                },
            },
        )
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "forbidden" in body["error"]["message"].lower()
    mock_replay.assert_not_awaited()


# ---------------------------------------------------------------------------
# meho_audit_replay — happy path + tenant scoping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_replay_admin_returns_tree_envelope(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``tenant_admin`` gets the ``{root, session_id, tenant_id, row_count}`` tree."""
    client, op = client_with_operator
    mock_replay = AsyncMock(return_value=_two_level_tree())
    mock_count = AsyncMock(return_value=2)
    with patch(_REPLAY_PATCH, new=mock_replay), patch(_COUNT_PATCH, new=mock_count):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {
                    "name": "meho_audit_replay",
                    "arguments": {"session_id": _SESSION_ID},
                },
            },
        )
    body = response.json()
    assert body["result"]["isError"] is False
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["session_id"] == _SESSION_ID
    assert payload["tenant_id"] == str(op.tenant_id)
    # row_count counts every node in the returned tree (root + child).
    assert payload["row_count"] == 2
    # #2776: the excluded-null tally rides on the same envelope; empty
    # audit_log in this test → zero.
    assert payload["excluded_null_session_count"] == 0
    assert len(payload["root"]) == 1
    assert payload["root"][0]["id"] == "11111111-1111-1111-1111-111111111111"
    assert payload["root"][0]["depth"] == 0
    assert len(payload["root"][0]["children"]) == 1
    assert payload["root"][0]["children"][0]["depth"] == 1


def _replay_seeded(client: TestClient, session_id: str) -> dict[str, object]:
    """Call ``meho_audit_replay`` with the substrate patched to an empty forest.

    ``replay_session`` returns ``[]`` and ``_count_session_rows`` returns
    ``0`` so ``row_count`` is 0; the real ``_count_excluded_null_session_rows``
    query runs against whatever rows the test seeded.
    """
    mock_replay = AsyncMock(return_value=[])
    mock_count = AsyncMock(return_value=0)
    with patch(_REPLAY_PATCH, new=mock_replay), patch(_COUNT_PATCH, new=mock_count):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 15,
                "method": "tools/call",
                "params": {
                    "name": "meho_audit_replay",
                    "arguments": {"session_id": session_id},
                },
            },
        )
    body = response.json()
    assert body["result"]["isError"] is False
    return json.loads(body["result"]["content"][0]["text"])


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_replay_admin_reports_excluded_null_session_count(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """#2700 → #2776: header-less MCP rows surface as ``excluded_null_session_count``.

    Mirrors the REST reference
    (``test_api_audit_routes.py::test_replay_reports_excluded_null_session_count``)
    on the MCP ``meho_audit_replay`` path: three header-less ``method=MCP``
    / null rows are counted; a ``method=POST`` null row and a different
    tenant's MCP-null row are excluded. The forest is empty (``row_count``
    0), so a non-zero tally proves an empty forest is NOT an empty history.
    """
    client, op = client_with_operator

    async def _seed() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            # Three header-less MCP calls: real operations, no negotiated session.
            for i in range(3):
                _add_audit_row(
                    session,
                    tenant_id=op.tenant_id,
                    second=i,
                    method="MCP",
                    agent_session_id=None,
                )
            # A non-MCP NULL-session row must NOT inflate the count.
            _add_audit_row(
                session,
                tenant_id=op.tenant_id,
                second=9,
                method="POST",
                agent_session_id=None,
            )
            # A different tenant's header-less MCP row must NOT leak in.
            _add_audit_row(
                session,
                tenant_id=_OTHER_TENANT_ID,
                second=0,
                method="MCP",
                agent_session_id=None,
            )

    asyncio.run(_seed())

    payload = _replay_seeded(client, _SESSION_ID)
    # Empty forest for this session id...
    assert payload["root"] == []
    assert payload["row_count"] == 0
    # ...but NOT an empty history: three header-less MCP rows are counted.
    # The POST row and tenant B's row are out.
    assert payload["excluded_null_session_count"] == 3


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_replay_admin_excluded_null_session_count_zero_base_case(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """No header-less MCP rows → the tally is 0 (empty forest IS empty history)."""
    client, _op = client_with_operator
    payload = _replay_seeded(client, _SESSION_ID)
    assert payload["row_count"] == 0
    assert payload["excluded_null_session_count"] == 0


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_replay_admin_tenant_scope_from_jwt_not_args(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Tenant scope is the JWT's — never an argument; cross-tenant is impossible.

    ``additionalProperties: false`` blocks smuggling a ``tenant_id``
    argument, and the substrate is dispatched with the operator's JWT
    tenant_id keyword. A tenant-B admin replaying a tenant-A session id
    sees only their own tenant boundary (the substrate returns empty,
    proven here by asserting the keyword passed to the substrate).
    """
    client, op = client_with_operator
    mock_replay = AsyncMock(return_value=[])
    mock_count = AsyncMock(return_value=0)
    with patch(_REPLAY_PATCH, new=mock_replay), patch(_COUNT_PATCH, new=mock_count):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 11,
                "method": "tools/call",
                "params": {
                    "name": "meho_audit_replay",
                    "arguments": {"session_id": _OTHER_SESSION_ID},
                },
            },
        )
    assert response.json()["result"]["isError"] is False
    assert mock_replay.await_args.kwargs["tenant_id"] == op.tenant_id
    assert op.tenant_id == OPERATOR_TENANT_ID
    # The count guard is also tenant-scoped to the JWT.
    assert mock_count.await_args.kwargs["tenant_id"] == op.tenant_id


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_replay_admin_max_depth_passed_through(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """An explicit ``max_depth`` reaches the substrate; default is 20."""
    client, _op = client_with_operator
    mock_replay = AsyncMock(return_value=[])
    mock_count = AsyncMock(return_value=0)
    with patch(_REPLAY_PATCH, new=mock_replay), patch(_COUNT_PATCH, new=mock_count):
        post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 12,
                "method": "tools/call",
                "params": {
                    "name": "meho_audit_replay",
                    "arguments": {"session_id": _SESSION_ID, "max_depth": 5},
                },
            },
        )
        assert mock_replay.await_args.kwargs["max_depth"] == 5

        post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 13,
                "method": "tools/call",
                "params": {
                    "name": "meho_audit_replay",
                    "arguments": {"session_id": _SESSION_ID},
                },
            },
        )
        assert mock_replay.await_args.kwargs["max_depth"] == 20


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_replay_admin_session_too_large_returns_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Count-first guard: an over-cap session is rejected with -32602 session_too_large.

    The substrate is never dispatched — the count short-circuits before
    the recursive walk runs.
    """
    client, _op = client_with_operator
    mock_replay = AsyncMock(return_value=_two_level_tree())
    mock_count = AsyncMock(return_value=10_001)
    with patch(_REPLAY_PATCH, new=mock_replay), patch(_COUNT_PATCH, new=mock_count):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 14,
                "method": "tools/call",
                "params": {
                    "name": "meho_audit_replay",
                    "arguments": {"session_id": _SESSION_ID},
                },
            },
        )
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "session_too_large" in body["error"]["message"]
    mock_replay.assert_not_awaited()


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_replay_admin_requires_session_id(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``session_id`` is required by the schema — omission is -32602."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 15,
            "method": "tools/call",
            "params": {"name": "meho_audit_replay", "arguments": {}},
        },
    )
    assert response.json()["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_replay_admin_rejects_additional_properties(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``additionalProperties: false`` blocks a smuggled ``tenant_id`` arg."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 16,
            "method": "tools/call",
            "params": {
                "name": "meho_audit_replay",
                "arguments": {
                    "session_id": _SESSION_ID,
                    "tenant_id": "11111111-1111-1111-1111-111111111111",
                },
            },
        },
    )
    assert response.json()["error"]["code"] == INVALID_PARAMS


# ---------------------------------------------------------------------------
# query_audit shape="tree" — operator self-session path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_query_audit_tree_own_session_returns_tree(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """An operator replaying their OWN bound session gets the tree.

    The ``Mcp-Session-Id`` header binds the caller's session id; passing
    the same id as ``agent_session_id`` with ``shape="tree"`` satisfies
    the self-session check and dispatches through the substrate.
    """
    client, op = client_with_operator
    mock_replay = AsyncMock(return_value=_two_level_tree())
    mock_count = AsyncMock(return_value=2)
    with patch(_REPLAY_PATCH, new=mock_replay), patch(_COUNT_PATCH, new=mock_count):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 20,
                "method": "tools/call",
                "params": {
                    "name": "query_audit",
                    "arguments": {"agent_session_id": _SESSION_ID, "shape": "tree"},
                },
            },
            headers={"Mcp-Session-Id": _SESSION_ID},
        )
    body = response.json()
    assert body["result"]["isError"] is False
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["session_id"] == _SESSION_ID
    assert payload["tenant_id"] == str(op.tenant_id)
    assert payload["row_count"] == 2
    # #2776: parity — the operator self-session tree branch carries the key too.
    assert payload["excluded_null_session_count"] == 0
    assert mock_replay.await_args.kwargs["tenant_id"] == op.tenant_id


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_query_audit_tree_foreign_session_returns_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``shape="tree"`` with a DIFFERENT session id than the caller's own → -32602.

    This is the self-session security contract: even though the flat path
    would happily return other in-tenant principals' rows, the tree path
    is locked to the caller's own session. The substrate is never reached.
    """
    client, _op = client_with_operator
    mock_replay = AsyncMock(return_value=_two_level_tree())
    mock_count = AsyncMock(return_value=2)
    with patch(_REPLAY_PATCH, new=mock_replay), patch(_COUNT_PATCH, new=mock_count):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 21,
                "method": "tools/call",
                "params": {
                    "name": "query_audit",
                    "arguments": {"agent_session_id": _OTHER_SESSION_ID, "shape": "tree"},
                },
            },
            headers={"Mcp-Session-Id": _SESSION_ID},
        )
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "own session" in body["error"]["message"].lower()
    mock_replay.assert_not_awaited()


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_query_audit_tree_without_agent_session_id_returns_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``shape="tree"`` with no ``agent_session_id`` → -32602 (must be self-targeted)."""
    client, _op = client_with_operator
    mock_replay = AsyncMock(return_value=_two_level_tree())
    with patch(_REPLAY_PATCH, new=mock_replay):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 22,
                "method": "tools/call",
                "params": {"name": "query_audit", "arguments": {"shape": "tree"}},
            },
            headers={"Mcp-Session-Id": _SESSION_ID},
        )
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "agent_session_id" in body["error"]["message"]
    mock_replay.assert_not_awaited()


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_query_audit_tree_without_session_header_returns_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """No ``Mcp-Session-Id`` header → no contextvar → -32602.

    With no header the transport does not bind the ``mcp_session_id``
    contextvar at all (G0.14-T6 #1147 decoupled capture from
    enforcement — no header, no synthetic id), so the self-session
    check at :func:`_resolve_self_session_id` reads ``None`` and
    rejects any ``agent_session_id`` the client supplies. The tree
    path stays self-session-only even when the caller didn't establish
    an explicit session.
    """
    client, _op = client_with_operator
    mock_replay = AsyncMock(return_value=_two_level_tree())
    with patch(_REPLAY_PATCH, new=mock_replay):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 23,
                "method": "tools/call",
                "params": {
                    "name": "query_audit",
                    "arguments": {"agent_session_id": _SESSION_ID, "shape": "tree"},
                },
            },
        )
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    mock_replay.assert_not_awaited()


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_query_audit_tree_session_too_large_returns_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """The self-session tree path shares the count-first 10k guard."""
    client, _op = client_with_operator
    mock_replay = AsyncMock(return_value=_two_level_tree())
    mock_count = AsyncMock(return_value=10_001)
    with patch(_REPLAY_PATCH, new=mock_replay), patch(_COUNT_PATCH, new=mock_count):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 24,
                "method": "tools/call",
                "params": {
                    "name": "query_audit",
                    "arguments": {"agent_session_id": _SESSION_ID, "shape": "tree"},
                },
            },
            headers={"Mcp-Session-Id": _SESSION_ID},
        )
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "session_too_large" in body["error"]["message"]
    mock_replay.assert_not_awaited()


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_query_audit_flat_shape_still_dispatches_normally(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``shape="flat"`` (and absent shape) leave the flat query path untouched.

    A regression guard: the tree branch must not swallow the default
    list path. ``replay_session`` is never reached for a flat call.
    """
    from meho_backplane.audit_query import AuditQueryResult

    client, _op = client_with_operator
    mock_replay = AsyncMock(return_value=_two_level_tree())
    mock_query = AsyncMock(return_value=AuditQueryResult(rows=[], next_cursor=None))
    with (
        patch(_REPLAY_PATCH, new=mock_replay),
        patch("meho_backplane.mcp.tools.audit.query_audit", new=mock_query),
    ):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 25,
                "method": "tools/call",
                "params": {
                    "name": "query_audit",
                    "arguments": {"shape": "flat", "agent_session_id": _SESSION_ID},
                },
            },
            headers={"Mcp-Session-Id": _SESSION_ID},
        )
    body = response.json()
    assert body["result"]["isError"] is False
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload == {"rows": [], "next_cursor": None}
    mock_replay.assert_not_awaited()
    mock_query.assert_awaited_once()
    # The flat path still sees agent_session_id as a substrate filter.
    assert mock_query.await_args.args[0].agent_session_id == uuid.UUID(_SESSION_ID)
