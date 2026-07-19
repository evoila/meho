# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""MCP-front tests for ``meho.topology.bulk_import`` (#2539, Initiative #2533).

Coverage (the MCP-level half — the batch service is covered in
:mod:`tests.test_topology_bulk_import`; the agent-park / approve-execute
loop is covered in :mod:`tests.test_topology_ops_approval`):

* Registration — ``required_role=TENANT_ADMIN`` + ``op_class='write'``;
  hidden from a non-admin ``tools/list``.
* Free dry-run — ``dry_run`` defaults to true; returns the per-row
  create/update/conflict plan, writes nothing (no ``graph_edge`` rows,
  no service audit rows), never parks.
* Human apply — a human tenant_admin ``dry_run=false`` applies the whole
  batch immediately (T3 dial inherited), landing every edge.
* Validation-failing batch — a row naming a missing endpoint surfaces
  every row's diagnostic together on the -32602 ``error.data`` (the
  REST ``422 invalid_bulk`` analogue) and writes nothing.
* Boundary cap — a batch over 1000 rows is rejected at the tool
  boundary (inputSchema ``maxItems``), the MCP analogue of the REST
  422 the ``_BULK_IMPORT_MAX_EDGES`` guard raises.

The happy paths exercise the SQLite-migrated test DB end-to-end (same
shape :mod:`tests.test_mcp_tools_topology_create_node` uses).
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
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.topology.schemas import BULK_IMPORT_MAX_EDGES
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, GraphEdge, GraphNode, Tenant
from meho_backplane.mcp.registry import get_tool
from meho_backplane.mcp.schemas import INVALID_PARAMS
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

# Broadcast publisher patch — the apply path fans out one event per row
# through the shared annotate helper.
_PUBLISH_PATCH = "meho_backplane.topology.annotate.publish_event"
_TOOL_NAME = "meho.topology.bulk_import"


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def _seeded_tenant() -> AsyncIterator[None]:
    """Insert the operator's :class:`Tenant` row so endpoint resolution finds it."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        session.add(Tenant(id=OPERATOR_TENANT_ID, slug="op-tenant", name="Op Tenant"))
    yield


async def _seed_node(*, kind: str, name: str) -> uuid.UUID:
    """Insert one ``graph_node`` row in the operator's tenant; return its id."""
    sessionmaker = get_sessionmaker()
    node_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            GraphNode(
                id=node_id,
                tenant_id=OPERATOR_TENANT_ID,
                kind=kind,
                name=name,
                target_id=None,
                properties={},
                discovered_by="test",
                first_seen=datetime.now(UTC),
            )
        )
        await session.commit()
    return node_id


def _bulk_import_call(client: TestClient, call_id: int, arguments: dict[str, Any]) -> Any:
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": call_id,
            "method": "tools/call",
            "params": {"name": _TOOL_NAME, "arguments": arguments},
        },
    )


async def _count_edges() -> int:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(GraphEdge).where(GraphEdge.tenant_id == OPERATOR_TENANT_ID)
                )
            )
            .scalars()
            .all()
        )
    return len(rows)


async def _count_service_audit_rows() -> int:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.path == "topology.annotate",
                        AuditLog.method == "ANNOTATE",
                    )
                )
            )
            .scalars()
            .all()
        )
    return len(rows)


# ---------------------------------------------------------------------------
# Registration + RBAC
# ---------------------------------------------------------------------------


def test_bulk_import_tool_registers_with_tenant_admin_and_write() -> None:
    """The tool lands with TENANT_ADMIN gate + write op_class."""
    entry = get_tool(_TOOL_NAME)
    assert entry is not None, f"{_TOOL_NAME} not registered"
    defn, _handler = entry
    assert defn.required_role == TenantRole.TENANT_ADMIN
    assert defn.op_class == "write"
    # dry_run defaults to true — the safe read-shaped plan is the default.
    assert defn.inputSchema["properties"]["dry_run"]["default"] is True
    # The rows array carries the boundary cap.
    assert defn.inputSchema["properties"]["rows"]["maxItems"] == BULK_IMPORT_MAX_EDGES


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_bulk_import_hidden_from_non_admin_tools_list(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """An operator-role session does not see the admin tool in ``tools/list``."""
    client, _op = client_with_operator
    response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert _TOOL_NAME not in names


# ---------------------------------------------------------------------------
# Free dry-run plan — no writes, never parks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
async def test_bulk_import_default_dry_run_returns_plan_and_writes_nothing(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    _seeded_tenant: None,
) -> None:
    """``dry_run`` omitted → plan returned, zero edges + zero service audit rows."""
    client, _op = client_with_operator
    await _seed_node(kind="service", name="svc-a")
    await _seed_node(kind="database", name="db-b")

    response = _bulk_import_call(
        client,
        10,
        {"rows": [{"from_name": "svc-a", "kind": "depends-on", "to_name": "db-b"}]},
    )

    body = response.json()
    assert body["result"]["isError"] is False, body
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["dry_run"] is True
    assert payload["created"] == 1
    assert payload["rows"][0]["action"] == "create"
    assert payload["rows"][0]["edge_id"] is None  # nothing created yet
    assert payload["rows"][0]["from_name"] == "svc-a"
    assert payload["rows"][0]["to_name"] == "db-b"

    # The read-shaped plan wrote nothing: no edge, no service audit row.
    assert await _count_edges() == 0
    assert await _count_service_audit_rows() == 0


# ---------------------------------------------------------------------------
# Human apply — immediate, atomic, all rows land
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
async def test_bulk_import_human_apply_lands_all_edges(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    _seeded_tenant: None,
) -> None:
    """A human tenant_admin ``dry_run=false`` applies the whole batch immediately."""
    client, _op = client_with_operator
    await _seed_node(kind="service", name="svc-a")
    await _seed_node(kind="database", name="db-b")
    await _seed_node(kind="database", name="db-c")

    with patch(_PUBLISH_PATCH, new=AsyncMock()):
        response = _bulk_import_call(
            client,
            20,
            {
                "dry_run": False,
                "rows": [
                    {"from_name": "svc-a", "kind": "depends-on", "to_name": "db-b"},
                    {"from_name": "svc-a", "kind": "depends-on", "to_name": "db-c"},
                ],
            },
        )

    body = response.json()
    assert body["result"]["isError"] is False, body
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["dry_run"] is False
    assert payload["created"] == 2
    # Applied rows carry real edge ids.
    assert all(uuid.UUID(r["edge_id"]) for r in payload["rows"])
    assert await _count_edges() == 2


# ---------------------------------------------------------------------------
# Validation failure — every row's diagnostic surfaced, nothing written
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
async def test_bulk_import_validation_failure_surfaces_per_row_diagnostics(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    _seeded_tenant: None,
) -> None:
    """A dry-run batch with a missing endpoint → -32602 with per-row errors, no writes."""
    client, _op = client_with_operator
    await _seed_node(kind="service", name="svc-a")

    response = _bulk_import_call(
        client,
        30,
        {
            "rows": [
                {"from_name": "svc-a", "kind": "depends-on", "to_name": "ghost-db"},
            ]
        },
    )

    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    data = body["error"]["data"]
    assert data["error"] == "invalid_bulk"
    assert len(data["errors"]) == 1
    assert data["errors"][0]["index"] == 0
    assert data["errors"][0]["error"] == "node_not_found"
    # Nothing written.
    assert await _count_edges() == 0
    assert await _count_service_audit_rows() == 0


# ---------------------------------------------------------------------------
# Boundary cap — over-1000 rows rejected before the handler runs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_bulk_import_over_cap_rejected_at_boundary(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A batch over the 1000-row cap fails the inputSchema maxItems (-32602)."""
    client, _op = client_with_operator
    oversized = [
        {"from_name": f"svc-{i}", "kind": "depends-on", "to_name": f"db-{i}"}
        for i in range(BULK_IMPORT_MAX_EDGES + 1)
    ]
    response = _bulk_import_call(client, 40, {"rows": oversized})
    assert response.json()["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_bulk_import_empty_batch_rejected_at_boundary(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """An empty ``rows`` array fails the inputSchema minItems (-32602)."""
    client, _op = client_with_operator
    response = _bulk_import_call(client, 41, {"rows": []})
    assert response.json()["error"]["code"] == INVALID_PARAMS
