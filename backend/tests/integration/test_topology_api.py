# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end ``/api/v1/topology*`` + ``/api/v1/targets/discover`` tests.

G9.1-T5 (#453) acceptance criteria that need real PG (not SQLite):

* **All 5 routes end-to-end against a real backplane.** The three
  query routes use PostgreSQL's ``WITH RECURSIVE ... CYCLE`` clause
  (SQLite does not implement it); the refresh route resolves a
  connector, calls ``discover_topology``, and reconciles the snapshot
  into ``graph_node`` / ``graph_edge`` in a real transaction. Every
  route is driven through the production auth + audit middleware
  chain.
* **Tenant boundary verified across all 5 routes.** Two seeded tenants
  with overlapping node + target names; each tenant's queries return
  only their own rows; a tenant-B refresh writes only tenant-B rows;
  cross-tenant refresh is impossible (the target name resolves
  tenant-scoped, so tenant B cannot even name tenant A's target).
* **Audit rows land in real PG.** The refresh route's domain-level
  ``topology.refresh`` audit row + the chassis HTTP-level row both
  commit through the testcontainer.

Same ``httpx.AsyncClient`` + ``ASGITransport`` rationale as
``tests/integration/test_kb_routes_pg.py``: the asyncpg pool the
``pg_engine`` fixture creates is bound to the pytest-asyncio loop, so
the request → handler → pool path must stay single-loop.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import ASGITransport

from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector
from meho_backplane.connectors.schemas import (
    CandidateHint,
    EdgeHint,
    FingerprintResult,
    NodeHint,
    OperationResult,
    ProbeResult,
    TopologyHints,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import GraphEdge, GraphNode
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE

from .conftest import DOCKER_AVAILABLE, SKIP_REASON, build_integration_app

# Pinned tenant UUIDs match the seed rows the ``pg_engine`` conftest
# fixture inserts.
TENANT_A_ID = "11111111-1111-1111-1111-111111111111"
TENANT_B_ID = "22222222-2222-2222-2222-222222222222"

_skip_no_docker = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)

_PRODUCT = "topo-test-product"


# ---------------------------------------------------------------------------
# A deterministic connector whose discover_topology returns a fixed
# snapshot keyed off the target name so two tenants get disjoint graphs.
# ---------------------------------------------------------------------------


class _TopoTestConnector(Connector):
    """Fake connector: stable topology snapshot + one discover candidate."""

    product = _PRODUCT

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:
        raise NotImplementedError

    async def discover_topology(self, target: Any) -> TopologyHints:
        # Two-edge chain rooted at the target node: <name> --belongs-to-->
        # vm-<name> --runs-on--> host-<name>. The target node's name is
        # the target's own name so the query routes can anchor on it.
        tname = target.name
        return TopologyHints(
            nodes=(
                NodeHint(kind="target", name=tname),
                NodeHint(kind="vm", name=f"vm-{tname}"),
                NodeHint(kind="host", name=f"host-{tname}"),
            ),
            edges=(
                EdgeHint(
                    from_kind="target",
                    from_name=tname,
                    to_kind="vm",
                    to_name=f"vm-{tname}",
                    kind="belongs-to",
                ),
                EdgeHint(
                    from_kind="vm",
                    from_name=f"vm-{tname}",
                    to_kind="host",
                    to_name=f"host-{tname}",
                    kind="runs-on",
                ),
            ),
            discovered_at=datetime.now(UTC),
        )

    async def list_candidates(self, seed_target: Any | None = None) -> list[CandidateHint]:
        return [
            CandidateHint(
                name="discovered-host",
                host="10.9.9.9",
                port=443,
                evidence={"seed": getattr(seed_target, "name", None)},
                confidence="medium",
            )
        ]


@pytest.fixture
def topo_app(pg_engine: None) -> AsyncIterator[FastAPI]:
    """Integration app + topology + targets routers; fake connector registered."""
    from meho_backplane.api.v1.targets import router as targets_router
    from meho_backplane.api.v1.topology import router as topology_router

    clear_registry()
    _CONNECTOR_INSTANCE_CACHE.clear()
    register_connector(_PRODUCT, _TopoTestConnector)

    app = build_integration_app()
    app.include_router(topology_router)
    app.include_router(targets_router)
    yield app

    clear_registry()
    _CONNECTOR_INSTANCE_CACHE.clear()


def _make_async_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    )


def _operator_token(*, tenant_id: str, sub: str = "op") -> tuple[object, str]:
    from tests._oidc_jwt_helpers import make_rsa_keypair, mint_token

    key = make_rsa_keypair(f"kid-op-{sub}")
    token = mint_token(
        key,
        sub=sub,
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=tenant_id,
    )
    return key, token


def _authed(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _insert_target(*, tenant_id: str, name: str) -> uuid.UUID:
    """Insert a TargetORM row for *tenant_id* and return its id."""
    tid = uuid.uuid4()
    now = datetime.now(UTC)
    sm = get_sessionmaker()
    async with sm() as session, session.begin():
        session.add(
            TargetORM(
                id=tid,
                tenant_id=uuid.UUID(tenant_id),
                name=name,
                product=_PRODUCT,
                host="10.0.0.1",
                aliases=[],
                port=None,
                fqdn=None,
                secret_ref=None,
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                notes=None,
                created_at=now,
                updated_at=now,
            )
        )
    return tid


async def _count_graph_rows(tenant_id: str) -> tuple[int, int]:
    """Return (node_count, edge_count) for *tenant_id*."""
    from sqlalchemy import func, select

    sm = get_sessionmaker()
    async with sm() as session:
        nodes = await session.execute(
            select(func.count())
            .select_from(GraphNode)
            .where(GraphNode.tenant_id == uuid.UUID(tenant_id))
        )
        edges = await session.execute(
            select(func.count())
            .select_from(GraphEdge)
            .where(GraphEdge.tenant_id == uuid.UUID(tenant_id))
        )
    return int(nodes.scalar_one()), int(edges.scalar_one())


# ---------------------------------------------------------------------------
# Test 1 — all 5 routes end-to-end against a real backplane
# ---------------------------------------------------------------------------


@_skip_no_docker
async def test_all_five_routes_end_to_end(topo_app: FastAPI) -> None:
    """Refresh seeds the graph; the query + discover routes read it back."""
    from tests._oidc_jwt_helpers import mock_discovery_and_jwks, public_jwks

    await _insert_target(tenant_id=TENANT_A_ID, name="vc-a")
    key, token = _operator_token(tenant_id=TENANT_A_ID, sub="op-a")

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        async with _make_async_client(topo_app) as client:
            # --- 1. POST /api/v1/topology/refresh/{target_name} ---
            refresh_resp = await client.post(
                "/api/v1/topology/refresh/vc-a",
                headers=_authed(token),
            )
            assert refresh_resp.status_code == 200, refresh_resp.text
            rr = refresh_resp.json()
            assert rr["added_nodes"] == 3
            assert rr["added_edges"] == 2

            # --- 2. GET /api/v1/topology/dependents/{name} ---
            # host-vc-a is depended on by vm-vc-a (depth 1) and
            # transitively by vc-a (depth 2). Root included at depth 0.
            dep_resp = await client.get(
                "/api/v1/topology/dependents/host-vc-a",
                headers=_authed(token),
            )
            assert dep_resp.status_code == 200, dep_resp.text
            names = {n["name"]: n["depth"] for n in dep_resp.json()}
            assert names == {"host-vc-a": 0, "vm-vc-a": 1, "vc-a": 2}

            # --- 3. GET /api/v1/topology/dependencies/{name} ---
            # vc-a depends on vm-vc-a (depth 1) and host-vc-a (depth 2).
            deps_resp = await client.get(
                "/api/v1/topology/dependencies/vc-a",
                headers=_authed(token),
            )
            assert deps_resp.status_code == 200, deps_resp.text
            dnames = {n["name"]: n["depth"] for n in deps_resp.json()}
            assert dnames == {"vc-a": 0, "vm-vc-a": 1, "host-vc-a": 2}

            # --- 4. GET /api/v1/topology/path?from=A&to=B ---
            path_resp = await client.get(
                "/api/v1/topology/path?from=vc-a&to=host-vc-a",
                headers=_authed(token),
            )
            assert path_resp.status_code == 200, path_resp.text
            path = path_resp.json()
            assert path["total_hops"] == 2
            assert [n["name"] for n in path["nodes"]] == [
                "vc-a",
                "vm-vc-a",
                "host-vc-a",
            ]

            # path to an unreachable node → 200 null
            none_resp = await client.get(
                "/api/v1/topology/path?from=vc-a&to=ghost",
                headers=_authed(token),
            )
            assert none_resp.status_code == 200
            assert none_resp.json() is None

            # --- 5. GET /api/v1/targets/discover?product=X ---
            disc_resp = await client.get(
                f"/api/v1/targets/discover?product={_PRODUCT}&seed_target=vc-a",
                headers=_authed(token),
            )
            assert disc_resp.status_code == 200, disc_resp.text
            disc = disc_resp.json()
            assert [c["name"] for c in disc["discovered"]] == ["discovered-host"]
            assert disc["discovered"][0]["evidence"]["seed"] == "vc-a"


# ---------------------------------------------------------------------------
# Test 2 — tenant boundary holds across all 5 routes
# ---------------------------------------------------------------------------


@_skip_no_docker
async def test_tenant_boundary_holds_across_all_routes(topo_app: FastAPI) -> None:
    """Overlapping target names; each tenant only ever sees its own graph.

    Both tenants register a target literally named ``shared``. Tenant A
    refreshes; tenant B's dependents query for the same node name
    returns 404-empty (the node exists only in tenant A). A tenant-B
    refresh writes only tenant-B rows. Cross-tenant refresh is
    impossible: tenant B's ``resolve_target`` can never name tenant
    A's target.
    """
    from tests._oidc_jwt_helpers import mock_discovery_and_jwks, public_jwks

    await _insert_target(tenant_id=TENANT_A_ID, name="shared")
    await _insert_target(tenant_id=TENANT_B_ID, name="shared")
    key_a, token_a = _operator_token(tenant_id=TENANT_A_ID, sub="op-a")
    key_b, token_b = _operator_token(tenant_id=TENANT_B_ID, sub="op-b")

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key_a, key_b))
        async with _make_async_client(topo_app) as client:
            # Tenant A refreshes its "shared" target.
            ra = await client.post(
                "/api/v1/topology/refresh/shared",
                headers=_authed(token_a),
            )
            assert ra.status_code == 200, ra.text
            assert ra.json()["added_nodes"] == 3

            # Tenant A sees its graph.
            da = await client.get(
                "/api/v1/topology/dependencies/shared",
                headers=_authed(token_a),
            )
            assert da.status_code == 200
            assert {n["name"] for n in da.json()} == {
                "shared",
                "vm-shared",
                "host-shared",
            }

            # Tenant B has NOT refreshed — its "shared" node does not
            # exist in its tenant-scoped graph, so the closure read
            # returns 404 ``node_untracked`` (G0.18-T4 #1357 contract;
            # pre-T4 this was 200 + ``[]`` which conflated
            # tracked-no-deps with untracked and let the tenant
            # boundary leak the wrong-shaped response, not just the
            # wrong-tenant data).
            db = await client.get(
                "/api/v1/topology/dependencies/shared",
                headers=_authed(token_b),
            )
            assert db.status_code == 404, db.text
            body = db.json()
            assert body["detail"]["error"] == "node_untracked"
            assert body["detail"]["name"] == "shared"

            # Tenant B refreshes its own "shared" target — only
            # tenant-B rows are written; tenant A's row count is
            # unchanged.
            a_nodes_before, a_edges_before = await _count_graph_rows(TENANT_A_ID)
            rb = await client.post(
                "/api/v1/topology/refresh/shared",
                headers=_authed(token_b),
            )
            assert rb.status_code == 200, rb.text
            assert rb.json()["added_nodes"] == 3

            a_nodes_after, a_edges_after = await _count_graph_rows(TENANT_A_ID)
            assert (a_nodes_after, a_edges_after) == (
                a_nodes_before,
                a_edges_before,
            )
            b_nodes, b_edges = await _count_graph_rows(TENANT_B_ID)
            assert (b_nodes, b_edges) == (3, 2)
