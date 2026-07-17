# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approval-gated agent authorship for the topology writes — #2537 (Initiative #2533).

The three curated-graph writes (``topology.annotate`` /
``topology.create_node`` / ``topology.unannotate``) are registered as
targetless typed ops (the ``secret.move`` mold, #1577) with
``safety_level="caution"`` + ``requires_approval=False`` — the
combination that parks AGENT principals (the needs-approval floor in
:mod:`meho_backplane.auth.permissions`) while humans ride the
default-allow branch. This module proves the full propose-then-approve
loop on the real dispatcher + approval queue:

* **Agent park, no write** — an AGENT-principal dispatch returns
  ``awaiting_approval``, creates exactly one PENDING
  :class:`~meho_backplane.db.models.ApprovalRequest` (``target_id``
  NULL — tenant-wide op), and writes nothing to the graph.
* **Four-eyes execute-once** — a *different* human operator approves
  via the real ``/decide`` REST route; the write lands with the exact
  original params; a second ``/decide`` returns HTTP 409 without a
  second write. The park→approve→execute cycle is proven for all three
  ops, ``create_node`` with a **novel open-vocabulary kind**
  (``dns-record`` — the #2534 + #2537 composition).
* **Humans stay immediate** — a human tenant_admin dispatch (and the
  MCP front handler) executes with no ApprovalRequest row created.
* **MCP front translation** — the ``meho.topology.*`` handlers return
  the executed domain shape for humans and the structured
  ``awaiting_approval`` envelope for agents.
* **Audit provenance** — topology write audit rows now stamp
  ``actor_sub`` from the RFC 8693 delegation contextvar (mirrors
  ``mcp/audit.py``).

Test isolation mirrors :mod:`tests.test_secret_move_approval`: the
autouse ``_default_database_url`` conftest fixture migrates the SQLite
DB to head; the two HTTP-level ``/decide`` tests drive the real FastAPI
app via ``TestClient`` with a minted operator JWT.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.delegation import actor_delegation
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.connectors.topology.ops import (
    TOPOLOGY_ANNOTATE_OP_ID,
    TOPOLOGY_CREATE_NODE_OP_ID,
    TOPOLOGY_GRAPH_CONNECTOR_ID,
    TOPOLOGY_UNANNOTATE_OP_ID,
    register_topology_graph_operations,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    ApprovalRequestStatus,
    AuditLog,
    GraphEdge,
    GraphNode,
    Tenant,
)
from meho_backplane.main import app
from meho_backplane.mcp.tools.topology import _annotate_handler
from meho_backplane.mcp.tools.topology_create_node import _create_node_handler
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks

_TENANT_ID = uuid.UUID(int=0)

_ANNOTATE_PARAMS: dict[str, Any] = {
    "from_name": "svc-payments",
    "kind": "depends-on",
    "to_name": "db-payments",
    "note": "asserted by agent proposal",
}


# ---------------------------------------------------------------------------
# Settings env + dispatcher isolation (mirrors test_secret_move_approval)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars Settings needs; reset caches around each test."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_dispatcher_caches()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_dispatcher_caches()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so registration doesn't pull ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def _registered_topology_ops(
    stub_embedding_service: AsyncMock,
) -> AsyncIterator[None]:
    """Upsert the three ``topology.*`` descriptor rows for dispatch tests."""
    await register_topology_graph_operations(embedding_service=stub_embedding_service)
    yield


@pytest.fixture
async def _seeded_tenant() -> AsyncIterator[None]:
    """Insert the operator's Tenant row so graph writes resolve the tenant."""
    async with get_sessionmaker()() as session, session.begin():
        session.add(Tenant(id=_TENANT_ID, slug="op-tenant", name="Op Tenant"))
    yield


def _make_operator(
    sub: str = "operator:human",
    *,
    principal_kind: PrincipalKind = PrincipalKind.USER,
    tenant_role: TenantRole = TenantRole.TENANT_ADMIN,
) -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="fake.jwt.value",
        tenant_id=_TENANT_ID,
        tenant_role=tenant_role,
        principal_kind=principal_kind,
    )


async def _seed_node(*, kind: str, name: str) -> uuid.UUID:
    """Insert one ``graph_node`` row in the test tenant; return its id."""
    node_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        session.add(
            GraphNode(
                id=node_id,
                tenant_id=_TENANT_ID,
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


async def _seed_edge_endpoints() -> None:
    await _seed_node(kind="service", name="svc-payments")
    await _seed_node(kind="database", name="db-payments")


async def _dispatch_topology(
    operator: Operator,
    op_id: str,
    params: dict[str, Any],
) -> OperationResult:
    """Dispatch a topology write through the real policy gate (no resume flag)."""
    return await dispatch(
        operator=operator,
        connector_id=TOPOLOGY_GRAPH_CONNECTOR_ID,
        op_id=op_id,
        target=None,
        params=params,
    )


async def _fetch_approval_rows() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        result = await session.execute(select(ApprovalRequest).order_by(ApprovalRequest.created_at))
        return list(result.scalars().all())


async def _fetch_edges() -> list[GraphEdge]:
    async with get_sessionmaker()() as session:
        result = await session.execute(select(GraphEdge))
        return list(result.scalars().all())


async def _fetch_nodes(kind: str) -> list[GraphNode]:
    async with get_sessionmaker()() as session:
        result = await session.execute(select(GraphNode).where(GraphNode.kind == kind))
        return list(result.scalars().all())


def _decide(request_id: uuid.UUID, *, decider_sub: str) -> Any:
    """Drive the real ``/decide`` REST route as *decider_sub* (four-eyes).

    Each call mints a fresh keypair under the same kid, so the JWKS
    cache from a prior call would fail signature verification — clear
    it before mounting the mocked discovery endpoints.
    """
    clear_jwks_cache()
    key = make_rsa_keypair("kid-topology-decider")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        token = mint_token(
            key,
            sub=decider_sub,
            tenant_role=TenantRole.OPERATOR.value,
            tenant_id=str(_TENANT_ID),
        )
        with TestClient(app) as client:
            return client.post(
                f"/api/v1/approvals/{request_id}/decide",
                headers={"Authorization": f"Bearer {token}"},
                json={"decision": "approved"},
            )


# ---------------------------------------------------------------------------
# Agent park: no write, one PENDING target-NULL row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_annotate_parks_pending_and_writes_nothing(
    _registered_topology_ops: None,
    _seeded_tenant: None,
) -> None:
    """An AGENT annotate parks (caution floor) and the edge is NOT written.

    ``safety_level="caution"`` + no AgentPermission grant → the
    needs-approval default parks the call. The pending row is
    tenant-wide (``target_id`` NULL — the targetless typed-op shape) and
    stores the original params for the by-id resume.
    """
    await _seed_edge_endpoints()
    agent = _make_operator("agent:proposer", principal_kind=PrincipalKind.AGENT)

    result = await _dispatch_topology(agent, TOPOLOGY_ANNOTATE_OP_ID, _ANNOTATE_PARAMS)

    assert result.status == "awaiting_approval", result.error
    rows = await _fetch_approval_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.status == ApprovalRequestStatus.PENDING.value
    assert row.op_id == TOPOLOGY_ANNOTATE_OP_ID
    assert row.connector_id == TOPOLOGY_GRAPH_CONNECTOR_ID
    assert row.target_id is None
    assert row.params == _ANNOTATE_PARAMS
    assert await _fetch_edges() == []


# ---------------------------------------------------------------------------
# Four-eyes /decide: execute once with the original params, then 409
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decide_executes_parked_annotate_once_then_409(
    _registered_topology_ops: None,
    _seeded_tenant: None,
) -> None:
    """A four-eyes ``/decide`` re-dispatches the parked annotate exactly once.

    The target-NULL resume path (#1503 stored params + #2293
    exactly-one-resumer claim) executes the edge write with the exact
    original params; a second ``/decide`` hits the already-decided guard
    (HTTP 409) and writes nothing more.
    """
    await _seed_edge_endpoints()
    agent = _make_operator("agent:proposer", principal_kind=PrincipalKind.AGENT)
    parked = await _dispatch_topology(agent, TOPOLOGY_ANNOTATE_OP_ID, _ANNOTATE_PARAMS)
    assert parked.status == "awaiting_approval", parked.error
    request_id = uuid.UUID(parked.extras["approval_request_id"])
    assert await _fetch_edges() == []

    first = _decide(request_id, decider_sub="operator:decider")
    assert first.status_code == 200, first.text
    assert first.json()["dispatch_status"] == "ok"

    edges = await _fetch_edges()
    assert len(edges) == 1
    edge = edges[0]
    assert edge.kind == "depends-on"
    assert edge.source == "curated"
    assert (edge.properties or {}).get("note") == "asserted by agent proposal"

    second = _decide(request_id, decider_sub="operator:decider")
    assert second.status_code == 409, second.text
    assert second.json() == {"detail": "approval_request_already_approved"}
    assert len(await _fetch_edges()) == 1


@pytest.mark.asyncio
async def test_create_node_park_approve_execute_with_novel_kind(
    _registered_topology_ops: None,
    _seeded_tenant: None,
) -> None:
    """park→approve→execute for ``topology.create_node`` with a novel kind.

    ``dns-record`` is not in the well-known set — the open vocabulary
    (#2534) composed with the approval gate (#2537): the agent proposes
    a novel-kind node, a human approves, the row lands.
    """
    agent = _make_operator("agent:proposer", principal_kind=PrincipalKind.AGENT)
    params = {"kind": "dns-record", "name": "api.example.com", "note": "seeded by agent"}

    parked = await _dispatch_topology(agent, TOPOLOGY_CREATE_NODE_OP_ID, params)
    assert parked.status == "awaiting_approval", parked.error
    assert await _fetch_nodes("dns-record") == []

    response = _decide(
        uuid.UUID(parked.extras["approval_request_id"]),
        decider_sub="operator:decider",
    )
    assert response.status_code == 200, response.text
    assert response.json()["dispatch_status"] == "ok"

    nodes = await _fetch_nodes("dns-record")
    assert len(nodes) == 1
    assert nodes[0].name == "api.example.com"


@pytest.mark.asyncio
async def test_unannotate_park_approve_execute(
    _registered_topology_ops: None,
    _seeded_tenant: None,
) -> None:
    """park→approve→execute for ``topology.unannotate``.

    A human tenant_admin asserts the curated edge immediately (the
    default-allow dispatch), the agent's revocation parks, the approval
    removes the edge.
    """
    await _seed_edge_endpoints()
    human = _make_operator("operator:asserter")
    asserted = await _dispatch_topology(human, TOPOLOGY_ANNOTATE_OP_ID, _ANNOTATE_PARAMS)
    assert asserted.status == "ok", asserted.error
    assert len(await _fetch_edges()) == 1

    agent = _make_operator("agent:proposer", principal_kind=PrincipalKind.AGENT)
    params = {"from_name": "svc-payments", "kind": "depends-on", "to_name": "db-payments"}
    parked = await _dispatch_topology(agent, TOPOLOGY_UNANNOTATE_OP_ID, params)
    assert parked.status == "awaiting_approval", parked.error
    assert len(await _fetch_edges()) == 1, "park must not remove the edge"

    response = _decide(
        uuid.UUID(parked.extras["approval_request_id"]),
        decider_sub="operator:decider",
    )
    assert response.status_code == 200, response.text
    assert response.json()["dispatch_status"] == "ok"
    assert await _fetch_edges() == []


# ---------------------------------------------------------------------------
# Humans stay immediate — no ApprovalRequest row on any human path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_human_dispatch_executes_immediately_no_approval_row(
    _registered_topology_ops: None,
    _seeded_tenant: None,
) -> None:
    """A human dispatch auto-executes: edge written, zero ApprovalRequest rows."""
    await _seed_edge_endpoints()
    human = _make_operator("operator:human")

    result = await _dispatch_topology(human, TOPOLOGY_ANNOTATE_OP_ID, _ANNOTATE_PARAMS)

    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["kind"] == "depends-on"
    assert result.result["source"] == "curated"
    assert len(await _fetch_edges()) == 1
    assert await _fetch_approval_rows() == []


@pytest.mark.asyncio
async def test_mcp_front_human_immediate_agent_parked(
    _registered_topology_ops: None,
    _seeded_tenant: None,
) -> None:
    """The MCP handlers keep humans immediate and park agents.

    * Human tenant_admin via ``_create_node_handler`` /
      ``_annotate_handler`` → executed domain shape (same keys as
      pre-#2537), no ApprovalRequest row.
    * Agent via ``_annotate_handler`` → structured ``awaiting_approval``
      envelope, edge not written.
    """
    human = _make_operator("operator:human")
    created = await _create_node_handler(human, {"kind": "service", "name": "svc-payments"})
    assert created["was_created"] is True
    assert created["kind"] == "service"
    await _seed_node(kind="database", name="db-payments")

    executed = await _annotate_handler(human, dict(_ANNOTATE_PARAMS))
    assert set(executed) == {"edge_id", "from", "to", "kind", "source", "conflicts"}
    assert executed["source"] == "curated"
    assert await _fetch_approval_rows() == []

    agent = _make_operator("agent:proposer", principal_kind=PrincipalKind.AGENT)
    parked = await _annotate_handler(
        agent,
        {"from_name": "svc-payments", "kind": "backed-up-by", "to_name": "db-payments"},
    )
    assert parked["status"] == "awaiting_approval"
    assert parked["op_id"] == TOPOLOGY_ANNOTATE_OP_ID
    rows = await _fetch_approval_rows()
    assert len(rows) == 1
    assert uuid.UUID(parked["approval_request_id"]) == rows[0].id
    edges = await _fetch_edges()
    assert [e.kind for e in edges] == ["depends-on"], "agent park must not write"


# ---------------------------------------------------------------------------
# Audit provenance — topology write rows stamp actor_sub
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_topology_write_audit_rows_carry_actor_sub(
    _registered_topology_ops: None,
    _seeded_tenant: None,
) -> None:
    """The service audit rows stamp ``actor_sub`` from the delegation context.

    Mirrors how ``mcp/audit.py`` populates the column: inside an RFC
    8693 delegation scope the acting principal lands on the row; outside
    it the column stays NULL (direct human requests, autonomous runs).
    """
    await _seed_edge_endpoints()
    human = _make_operator("operator:human")

    with actor_delegation("user:boss@example.com"):
        result = await _dispatch_topology(human, TOPOLOGY_ANNOTATE_OP_ID, _ANNOTATE_PARAMS)
    assert result.status == "ok", result.error

    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.path == TOPOLOGY_ANNOTATE_OP_ID,
                        AuditLog.method == "ANNOTATE",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].actor_sub == "user:boss@example.com"
    assert rows[0].operator_sub == "operator:human"
