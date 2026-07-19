# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the #2485 ``meho.topology.delete_node`` admin tool.

Coverage matrix (Task #2485 acceptance criteria — the MCP-level half;
the substrate is covered in :mod:`tests.test_topology_delete_node`, the
REST route in :mod:`tests.test_api_v1_topology`):

* The tool registers with ``required_role=TENANT_ADMIN`` and
  ``op_class='write'``; non-admin sessions do not see it in
  ``tools/list`` and a direct ``tools/call`` from an operator returns
  -32602 ``forbidden``.
* ``tools/call meho.topology.delete_node {node_id}`` on a manually-
  seeded (``source='curated'``) node hard-deletes the row and returns
  ``{node_id, kind, name}``. A second call returns -32602 (gone).
* A probe-owned (``source='auto'``) node is refused with -32602
  ``probe-owned`` — the schema-grounded guard.
* A malformed ``node_id`` and a smuggled ``tenant_id`` both surface as
  -32602 at the boundary.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import GraphNode, Tenant
from meho_backplane.mcp.registry import get_tool
from meho_backplane.mcp.schemas import INVALID_PARAMS
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_PUBLISH_DELETE_PATCH = "meho_backplane.topology.node_delete.publish_event"


@pytest_asyncio.fixture
async def _seeded_tenant() -> AsyncIterator[None]:
    """Insert the operator's :class:`Tenant` row so node FKs resolve."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        session.add(Tenant(id=OPERATOR_TENANT_ID, slug="op-tenant", name="Op Tenant"))
    yield


async def _seed_node(*, kind: str, name: str, source: str) -> uuid.UUID:
    """Insert one ``graph_node`` row directly; return its id."""
    node_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        session.add(
            GraphNode(
                id=node_id,
                tenant_id=OPERATOR_TENANT_ID,
                kind=kind,
                name=name,
                target_id=None,
                source=source,
                properties={},
                discovered_by="op-1" if source == "curated" else "vmware",
                first_seen=datetime.now(UTC),
                last_seen=datetime.now(UTC),
            )
        )
    return node_id


def _delete_node_call(client: TestClient, call_id: int, arguments: dict[str, Any]) -> Any:
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": call_id,
            "method": "tools/call",
            "params": {"name": "meho.topology.delete_node", "arguments": arguments},
        },
    )


# ---------------------------------------------------------------------------
# Registration + RBAC
# ---------------------------------------------------------------------------


def test_delete_node_tool_registers_with_tenant_admin_and_write() -> None:
    """The admin tool lands with TENANT_ADMIN gate + write op_class."""
    entry = get_tool("meho.topology.delete_node")
    assert entry is not None, "meho.topology.delete_node not registered"
    defn, _handler = entry
    assert defn.required_role == TenantRole.TENANT_ADMIN
    assert defn.op_class == "write"


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_delete_node_hidden_from_non_admin_tools_list(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """An operator-role session does not see the admin tool in ``tools/list``."""
    client, _op = client_with_operator
    response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert "meho.topology.delete_node" not in names
    assert "query_topology" in names


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_delete_node_visible_to_tenant_admin(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A tenant_admin session sees the admin tool in ``tools/list``."""
    client, _op = client_with_operator
    response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert "meho.topology.delete_node" in names


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_delete_node_call_from_non_admin_is_forbidden(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """tools/call meho.topology.delete_node from an operator → -32602 forbidden."""
    client, _op = client_with_operator
    response = _delete_node_call(client, 3, {"node_id": str(uuid.uuid4())})
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "forbidden" in body["error"]["message"].lower()


# ---------------------------------------------------------------------------
# Happy path + guards — end-to-end via the SQLite test DB
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
async def test_delete_node_removes_curated_row(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    _seeded_tenant: None,
) -> None:
    """tools/call deletes a manual seed and returns {node_id, kind, name}."""
    client, _op = client_with_operator
    node_id = await _seed_node(kind="vault-role", name="rdc-vault", source="curated")

    with patch(_PUBLISH_DELETE_PATCH, new=AsyncMock()) as publish_mock:
        response = _delete_node_call(client, 10, {"node_id": str(node_id)})

    body = response.json()
    assert body["result"]["isError"] is False, body
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["node_id"] == str(node_id)
    assert payload["kind"] == "vault-role"
    assert payload["name"] == "rdc-vault"
    assert publish_mock.await_count == 1

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        assert await session.get(GraphNode, node_id) is None


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
async def test_delete_node_second_call_returns_not_found(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    _seeded_tenant: None,
) -> None:
    """A second delete of the same id → -32602 (node gone)."""
    client, _op = client_with_operator
    node_id = await _seed_node(kind="vault-role", name="once", source="curated")

    with patch(_PUBLISH_DELETE_PATCH, new=AsyncMock()):
        first = _delete_node_call(client, 20, {"node_id": str(node_id)})
        second = _delete_node_call(client, 21, {"node_id": str(node_id)})

    assert first.json()["result"]["isError"] is False
    body = second.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "no graph_node matched" in body["error"]["message"]


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
async def test_delete_node_refuses_probe_owned(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    _seeded_tenant: None,
) -> None:
    """A probe-derived (``source='auto'``) node is refused with -32602."""
    client, _op = client_with_operator
    node_id = await _seed_node(kind="vm", name="probe-vm", source="auto")

    with patch(_PUBLISH_DELETE_PATCH, new=AsyncMock()):
        response = _delete_node_call(client, 30, {"node_id": str(node_id)})

    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "probe-owned" in body["error"]["message"]

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        assert await session.get(GraphNode, node_id) is not None


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_delete_node_rejects_malformed_uuid(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A non-UUID ``node_id`` surfaces as -32602."""
    client, _op = client_with_operator
    response = _delete_node_call(client, 40, {"node_id": "not-a-uuid"})
    assert response.json()["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_delete_node_rejects_additional_properties(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A smuggled ``tenant_id`` → -32602 (additionalProperties=false)."""
    client, _op = client_with_operator
    response = _delete_node_call(
        client, 41, {"node_id": str(uuid.uuid4()), "tenant_id": str(uuid.uuid4())}
    )
    assert response.json()["error"]["code"] == INVALID_PARAMS
