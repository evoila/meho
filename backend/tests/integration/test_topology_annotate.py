# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Cross-cutting integration tests for the G9.2 annotate / unannotate surface.

Task #601 (G9.2-T9) acceptance suite. The four invariants below land in
six discrete tests, all driven through the production REST + service
chain against a real ``pgvector/pgvector:pg16`` container:

* **§6 conflict rules** — same-kind / different-endpoint marks the auto
  row ``properties.superseded_by`` (sticky across refresh, cleared only
  by ``unannotate``); incompatible kinds over the same endpoint pair
  gain bidirectional ``properties.conflicts_with`` markers. Both rows
  persist; ``GET /edges?conflicts=true`` returns exactly the conflicted
  pair.
* **§11 tenant boundary** — a tenant-A annotation that names a tenant-B
  node 404s on resolve; ``GET /edges`` never leaks a cross-tenant row.
* **§3 auto-deletion rule** — ``DELETE /edges/{id}`` on a
  ``source='auto'`` row returns 409 with the rule message; a curated
  row 204s and clears any reciprocal markers it left.
* **§5 role gating** — ``operator``-role POST / DELETE → 403,
  ``read_only`` GET → 403, ``tenant_admin`` succeeds.

Why integration over unit-level: the unit-level suite at
``backend/tests/test_topology_annotate.py`` (G9.2-T3, #595) drives the
service primitive against the autouse SQLite test DB and covers the
service-level conflict mechanics exhaustively; the SQLite coverage of
the conflict logic the issue body anticipates is already on the tree.
This suite proves the same invariants survive at the production
boundary — RBAC dependency, audit middleware, broadcast publish, real
JSONB column semantics for the ``superseded_by`` / ``conflicts_with``
markers (SQLite's JSON1 round-trips the values but does not enforce the
``jsonb_typeof`` predicates the substrate's ``conflicts_only=True``
filter uses).

Why every test body is ``async def`` with no ``@pytest.mark.asyncio``:
``backend/pyproject.toml`` pins ``asyncio_mode = "auto"`` so the
plugin treats every ``async def`` test as a coroutine test on the
session loop the ``pg_engine`` asyncpg pool is bound to — same shape
as the rest of ``tests/integration/``.

Docker availability: the testcontainers-PG fixture (in
``conftest.py``) skips cleanly on agent sandboxes without a Docker
socket; CI runners provision Docker so the whole class runs there.
The collection-time smoke test at the end of the module runs
regardless and guards against accidental renames of the public surface.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import func, select

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
from meho_backplane.db.models import AuditLog, GraphEdge, GraphNode
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE

from .conftest import DOCKER_AVAILABLE, SKIP_REASON, build_integration_app

# Pinned tenant UUIDs match the seed rows the ``pg_engine`` conftest
# fixture inserts.
TENANT_A_ID = "11111111-1111-1111-1111-111111111111"
TENANT_B_ID = "22222222-2222-2222-2222-222222222222"

_skip_no_docker = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)

_PRODUCT = "annotate-test-product"

# Patch site for the broadcast publish: ``from ... import publish_event``
# rebinds the symbol into ``meho_backplane.topology.annotate``'s module
# dict, so the patchable reference is the *importer's* name. Mocking
# here keeps the test self-contained (no NATS / fan-out infra) while
# still proving the service called the publisher exactly once per write.
_PUBLISH_PATCH = "meho_backplane.topology.annotate.publish_event"


# ---------------------------------------------------------------------------
# A deterministic connector. The conflict-fixture tests don't drive the
# refresh route, but the §6 "sticky-supersede across refresh" test does:
# it calls ``refresh_target_topology`` directly with the same ``EdgeHint``
# the auto edge originally came from, so the connector's snapshot must
# match the seeded row's ``(from, to, kind)`` triple deterministically.
# ---------------------------------------------------------------------------


class _StickyTopoConnector(Connector):
    """Connector whose ``discover_topology`` matches the seeded auto edge.

    The §6 sticky-supersede test seeds an auto edge ``vm-A → host-X``
    of kind ``runs-on``, annotates a curated competitor ``vm-A →
    host-Y``, then simulates the next probe re-seeing the original
    auto edge by running the refresh against this connector. The
    snapshot returned here is the same ``(vm-A → host-X, runs-on)``
    plus the curated competitor's endpoints so neither node is
    GC'd by the reconcile pass.
    """

    product = _PRODUCT

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:
        raise NotImplementedError

    async def discover_topology(self, target: Any) -> TopologyHints:
        return TopologyHints(
            nodes=(
                NodeHint(kind="vm", name="vm-A"),
                NodeHint(kind="host", name="host-X"),
                NodeHint(kind="host", name="host-Y"),
            ),
            edges=(
                EdgeHint(
                    from_kind="vm",
                    from_name="vm-A",
                    to_kind="host",
                    to_name="host-X",
                    kind="runs-on",
                ),
            ),
            discovered_at=datetime.now(UTC),
        )

    async def list_candidates(self, seed_target: Any | None = None) -> list[CandidateHint]:
        return []


# ---------------------------------------------------------------------------
# App + JWT + HTTP plumbing
# ---------------------------------------------------------------------------


@pytest.fixture
def annotate_app(pg_engine: None) -> AsyncIterator[FastAPI]:
    """Integration app + topology router; fake connector registered.

    Mirrors :mod:`tests.integration.test_topology_api`'s ``topo_app``
    shape so the seeded tenants, the engine cache wiring, and the
    middleware stack stay identical across the two integration suites.
    """
    from meho_backplane.api.v1.topology import router as topology_router

    clear_registry()
    _CONNECTOR_INSTANCE_CACHE.clear()
    register_connector(_PRODUCT, _StickyTopoConnector)

    app = build_integration_app()
    app.include_router(topology_router)
    yield app

    clear_registry()
    _CONNECTOR_INSTANCE_CACHE.clear()


def _make_async_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    )


def _token(
    *,
    role: TenantRole,
    tenant_id: str,
    sub: str = "op",
) -> tuple[object, str]:
    """Mint an RS256 JWT for *role* / *tenant_id*; return (key, token).

    The integration env's ``KEYCLOAK_*`` env vars (set by
    :func:`integration_env` in ``conftest.py``) wire the chassis JWT
    verifier at the same issuer / audience the helper signs against.
    """
    from tests._oidc_jwt_helpers import make_rsa_keypair, mint_token

    key = make_rsa_keypair(f"kid-{role.value}-{sub}")
    token = mint_token(
        key,
        sub=sub,
        tenant_role=role.value,
        tenant_id=tenant_id,
    )
    return key, token


def _authed(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# DB seeding helpers
# ---------------------------------------------------------------------------


async def _seed_node(
    *,
    tenant_id: str,
    kind: str,
    name: str,
) -> uuid.UUID:
    """Insert one ``graph_node`` row and return its id."""
    nid = uuid.uuid4()
    sm = get_sessionmaker()
    async with sm() as session, session.begin():
        session.add(
            GraphNode(
                id=nid,
                tenant_id=uuid.UUID(tenant_id),
                kind=kind,
                name=name,
                target_id=None,
                properties={},
                discovered_by="test",
                first_seen=datetime.now(UTC),
            )
        )
    return nid


async def _seed_auto_edge(
    *,
    tenant_id: str,
    from_id: uuid.UUID,
    to_id: uuid.UUID,
    kind: str,
) -> uuid.UUID:
    """Insert one ``source='auto'`` ``graph_edge`` row and return its id.

    ``first_seen`` / ``last_seen`` are populated so the
    ``last_seen IS NOT NULL`` filter in :func:`list_edges` surfaces
    the row; an unset ``last_seen`` would be treated as soft-deleted.
    """
    eid = uuid.uuid4()
    now = datetime.now(UTC)
    sm = get_sessionmaker()
    async with sm() as session, session.begin():
        session.add(
            GraphEdge(
                id=eid,
                tenant_id=uuid.UUID(tenant_id),
                from_node_id=from_id,
                to_node_id=to_id,
                kind=kind,
                source="auto",
                properties={},
                discovered_by="test",
                first_seen=now,
                last_seen=now,
            )
        )
    return eid


async def _insert_target(*, tenant_id: str, name: str) -> uuid.UUID:
    """Insert a :class:`Target` row pinned at this product / tenant."""
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


async def _get_edge_props(edge_id: uuid.UUID) -> dict[str, Any]:
    """Read ``graph_edge.properties`` directly from PG.

    Re-reads through a fresh session so the test sees the row as a
    later HTTP request would, not a stale in-flight transaction.
    """
    sm = get_sessionmaker()
    async with sm() as session:
        edge = await session.get(GraphEdge, edge_id)
        assert edge is not None, f"edge {edge_id} disappeared"
        return dict(edge.properties or {})


async def _service_audit_rows(op_id: str, tenant_id: str) -> list[AuditLog]:
    """Read the *service-level* audit rows for *op_id* in *tenant_id*.

    The annotate / unannotate substrate writes its own audit row with
    ``method`` in ``{'ANNOTATE', 'UNANNOTATE'}`` and ``path`` set to
    the canonical ``op_id`` (``'topology.annotate'`` /
    ``'topology.unannotate'``); the chassis ``AuditMiddleware`` writes
    a second row for the same HTTP call with ``method='POST'`` /
    ``method='DELETE'`` and the URL path. This helper filters to the
    service row so the per-write count assertion stays single-axis.
    """
    sm = get_sessionmaker()
    async with sm() as session:
        result = await session.execute(
            select(AuditLog).where(
                AuditLog.path == op_id,
                AuditLog.tenant_id == uuid.UUID(tenant_id),
                AuditLog.method.in_(("ANNOTATE", "UNANNOTATE")),
            )
        )
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Conflict fixture builder — reusable across the two §6 tests
# ---------------------------------------------------------------------------


async def _seed_same_kind_conflict_fixture(
    tenant_id: str,
) -> dict[str, uuid.UUID]:
    """Seed the same-kind / different-endpoint §6 fixture.

    Shape (kind = ``runs-on``)::

        vm-A --auto--> host-X       (auto edge to be marked superseded)
        host-Y                       (curated competitor's target)

    The annotate flow under test seeds the curated row
    ``vm-A --runs-on--> host-Y``; the auto edge should pick up
    ``properties.superseded_by = <curated-id>`` after.
    """
    vm_a = await _seed_node(tenant_id=tenant_id, kind="vm", name="vm-A")
    host_x = await _seed_node(tenant_id=tenant_id, kind="host", name="host-X")
    host_y = await _seed_node(tenant_id=tenant_id, kind="host", name="host-Y")
    auto_edge = await _seed_auto_edge(
        tenant_id=tenant_id,
        from_id=vm_a,
        to_id=host_x,
        kind="runs-on",
    )
    return {"vm_a": vm_a, "host_x": host_x, "host_y": host_y, "auto_edge": auto_edge}


async def _seed_incompatible_kinds_fixture(
    tenant_id: str,
) -> dict[str, uuid.UUID]:
    """Seed the incompatible-kinds §6 fixture.

    Shape::

        svc --auto routes-through--> db   (auto edge survives)

    The annotate flow under test seeds the curated row
    ``svc --depends-on--> db`` over the same endpoint pair; both rows
    persist with bidirectional ``conflicts_with`` markers.
    """
    svc = await _seed_node(tenant_id=tenant_id, kind="service", name="svc")
    # ``volume`` is the closest in-vocabulary kind to "stateful storage
    # node" for this scenario; the closed v0.2 ``_GRAPH_NODE_KINDS``
    # (mirrored in migration 0007's ``_NODE_KINDS``) does not include
    # ``database``. Widening the vocabulary is a coordinated DB + model
    # migration scoped to G9.2's curated extensions, not a test-only
    # change. The variable name stays ``db`` because the test scenario
    # narrates "service routes through the database-shaped node" — the
    # *kind* slot just needs an in-vocab member; the conflict logic
    # under test is edge-kind-driven, not node-kind-driven.
    db = await _seed_node(tenant_id=tenant_id, kind="volume", name="db")
    auto_edge = await _seed_auto_edge(
        tenant_id=tenant_id,
        from_id=svc,
        to_id=db,
        kind="routes-through",
    )
    return {"svc": svc, "db": db, "auto_edge": auto_edge}


# ---------------------------------------------------------------------------
# §6 Conflict — same kind / different endpoint
# ---------------------------------------------------------------------------


@_skip_no_docker
async def test_conflict_same_kind_marks_auto_superseded_and_survives_refresh(
    annotate_app: FastAPI,
) -> None:
    """Annotate a curated edge over a competing auto edge → supersede mark.

    Drives every leg of the §6 same-kind rule end-to-end:

    1. The annotate POST stamps ``properties.superseded_by =
       <curated-id>`` on the auto row, and the recursive-CTE
       supersede filter inside :func:`find_dependents` /
       :func:`find_dependencies` / :func:`find_path` removes the
       superseded edge from every traversal in both directions.
    2. A second probe (simulated by calling
       :func:`refresh_target_topology` against a connector that
       re-emits the same auto edge hint with empty properties) does
       **not** clear the supersede marker. The refresh path's
       :func:`_merge_edge_properties` preserves the reserved keys;
       the traversal exclusion is still in force after the refresh.
    3. ``unannotate`` of the curated row clears the supersede mark
       on the auto row — the row is now visible to a fresh
       ``list_edges`` call with no markers, and the traversal
       closures + shortest-path query walk the edge again.
    """
    from tests._oidc_jwt_helpers import mock_discovery_and_jwks, public_jwks

    nodes = await _seed_same_kind_conflict_fixture(TENANT_A_ID)
    target_id = await _insert_target(tenant_id=TENANT_A_ID, name="vc-a")

    key, token = _token(role=TenantRole.TENANT_ADMIN, tenant_id=TENANT_A_ID, sub="op-a")

    # Build the same Operator the route handler would have passed;
    # tenant_admin is fine — the refresh is not role-gated below the
    # service primitive, and ``find_dependents`` / ``find_dependencies``
    # only use the tenant scope from this operator.
    from meho_backplane.auth.operator import Operator

    operator = Operator(
        sub="op-a",
        name=None,
        email=None,
        raw_jwt="not-a-real-jwt",
        tenant_id=uuid.UUID(TENANT_A_ID),
        tenant_role=TenantRole.TENANT_ADMIN,
    )

    with patch(_PUBLISH_PATCH, new_callable=AsyncMock), respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        async with _make_async_client(annotate_app) as client:
            # --- 1. Annotate the curated competitor ---
            ann_resp = await client.post(
                "/api/v1/topology/edges",
                json={
                    "from": {"name": "vm-A", "kind": "vm"},
                    "kind": "runs-on",
                    "to": {"name": "host-Y", "kind": "host"},
                    "note": "operator-asserted host placement",
                },
                headers=_authed(token),
            )
            assert ann_resp.status_code == 201, ann_resp.text
            curated_id = uuid.UUID(ann_resp.json()["id"])

            # The auto edge picked up the supersede marker pointing at
            # the curated row. Reading ``properties`` directly (not via
            # ``list_edges``) keeps the assertion narrow.
            props_after_annotate = await _get_edge_props(nodes["auto_edge"])
            assert props_after_annotate.get("superseded_by") == str(curated_id), (
                "auto edge should carry properties.superseded_by pointing at the curated row"
            )

            # Traversal must already exclude the superseded auto edge:
            # the recursive CTE filter
            # ``e.properties->>'superseded_by' IS NULL`` (query.py L220 /
            # L270) drops the row from forward + reverse closures alike.
            # ``host-X`` should not appear as a dependency of ``vm-A``
            # (no surviving outbound edge), and ``vm-A`` should not
            # appear as a dependent of ``host-X`` (no surviving inbound
            # edge). The curated competitor ``host-Y`` still does.
            from meho_backplane.topology.query import (
                find_dependencies,
                find_dependents,
                find_path,
            )

            deps_of_vm_a = await find_dependencies(operator, "vm-A", kind="vm")
            names_of_vm_a = {n.name for n in deps_of_vm_a}
            assert "host-X" not in names_of_vm_a, (
                "superseded auto edge must drop out of find_dependencies"
            )
            assert "host-Y" in names_of_vm_a, (
                "curated competitor must remain visible to find_dependencies"
            )

            dependents_of_host_x = await find_dependents(operator, "host-X", kind="host")
            names_into_host_x = {n.name for n in dependents_of_host_x}
            assert "vm-A" not in names_into_host_x, (
                "superseded auto edge must drop out of find_dependents"
            )

            # Bidirectional shortest path crosses the same supersede
            # filter (query.py L439 / L444 inside the ``bi_edge`` CTE),
            # so the only surviving route from ``vm-A`` to ``host-X`` is
            # "no route" — ``find_path`` returns ``None``.
            assert (
                await find_path(operator, "vm-A", "host-X", from_kind="vm", to_kind="host") is None
            ), "find_path must not walk through a superseded auto edge"

            # --- 2. Simulate the next refresh re-seeing the auto edge ---
            # Calling refresh_target_topology directly is honest end-to-end —
            # the production scheduler path runs the same _reconcile_edges
            # under the hood. The connector's hint has empty properties, so
            # a pre-#595 wholesale overwrite would have erased the marker.
            #
            # The production signature is ``(target, operator)`` where
            # ``target`` is the ``Target`` ORM row (refresh.py L589); we
            # load it through a fresh session and let the function open
            # its own transactions inside ``_apply_reconcile``. Broadcast
            # is patched alongside the annotate publish hook so the
            # post-commit publish in refresh.py does not exercise the
            # real event bus inside the test.
            from meho_backplane.topology.refresh import refresh_target_topology

            sm = get_sessionmaker()
            async with sm() as session:
                target_row = (
                    await session.execute(select(TargetORM).where(TargetORM.id == target_id))
                ).scalar_one()

            with patch(
                "meho_backplane.topology.refresh.publish_event",
                new_callable=AsyncMock,
            ):
                await refresh_target_topology(target_row, operator)

            props_after_refresh = await _get_edge_props(nodes["auto_edge"])
            assert props_after_refresh.get("superseded_by") == str(curated_id), (
                "supersede marker must survive the next refresh (sticky §6)"
            )

            # Traversal exclusion is still in force after the refresh —
            # the auto edge stayed superseded, so the closures still
            # omit ``vm-A → host-X``.
            deps_of_vm_a = await find_dependencies(operator, "vm-A", kind="vm")
            names_of_vm_a = {n.name for n in deps_of_vm_a}
            assert "host-X" not in names_of_vm_a, (
                "auto edge must remain hidden from find_dependencies after refresh"
            )

            dependents_of_host_x = await find_dependents(operator, "host-X", kind="host")
            names_into_host_x = {n.name for n in dependents_of_host_x}
            assert "vm-A" not in names_into_host_x, (
                "auto edge must remain hidden from find_dependents after refresh"
            )

            # --- 3. Unannotate the curated row → auto marker cleared ---
            del_resp = await client.delete(
                f"/api/v1/topology/edges/{curated_id}",
                headers=_authed(token),
            )
            assert del_resp.status_code == 204, del_resp.text

            props_after_unannotate = await _get_edge_props(nodes["auto_edge"])
            assert "superseded_by" not in props_after_unannotate, (
                "unannotate should clear the reciprocal supersede marker on the auto row"
            )

            # With the supersede marker gone, the auto edge is once
            # again visible to traversal in both directions, and the
            # shortest-path query now finds a single-hop route
            # ``vm-A → host-X``. This is the restoration leg of the
            # traversal-visibility invariant the issue's test matrix
            # enumerates for the same-kind §6 case.
            deps_of_vm_a = await find_dependencies(operator, "vm-A", kind="vm")
            names_of_vm_a = {n.name for n in deps_of_vm_a}
            assert "host-X" in names_of_vm_a, (
                "cleared auto edge must re-appear in find_dependencies"
            )

            dependents_of_host_x = await find_dependents(operator, "host-X", kind="host")
            names_into_host_x = {n.name for n in dependents_of_host_x}
            assert "vm-A" in names_into_host_x, (
                "cleared auto edge must re-appear in find_dependents"
            )

            restored_path = await find_path(
                operator, "vm-A", "host-X", from_kind="vm", to_kind="host"
            )
            assert restored_path is not None, (
                "find_path must walk the restored auto edge after unannotate"
            )


# ---------------------------------------------------------------------------
# §6 Conflict — incompatible kinds
# ---------------------------------------------------------------------------


@_skip_no_docker
async def test_conflict_incompatible_kinds_persists_both_rows_with_bidirectional_markers(
    annotate_app: FastAPI,
) -> None:
    """Annotate an incompatible-kind edge over the same endpoint pair.

    Both rows must persist (different kinds → not the same row under
    the ``(tenant, from, to, kind)`` unique index). Each row's
    ``properties.conflicts_with`` carries the other's id. The
    ``GET /api/v1/topology/edges?conflicts=true`` filter returns
    exactly the two conflicted rows.
    """
    from tests._oidc_jwt_helpers import mock_discovery_and_jwks, public_jwks

    nodes = await _seed_incompatible_kinds_fixture(TENANT_A_ID)

    key, token = _token(role=TenantRole.TENANT_ADMIN, tenant_id=TENANT_A_ID, sub="op-a")

    with patch(_PUBLISH_PATCH, new_callable=AsyncMock), respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        async with _make_async_client(annotate_app) as client:
            # Annotate the curated competitor (depends-on over the same pair).
            ann_resp = await client.post(
                "/api/v1/topology/edges",
                json={
                    "from": {"name": "svc", "kind": "service"},
                    "kind": "depends-on",
                    # ``volume`` matches the seed in
                    # :func:`_seed_incompatible_kinds_fixture`; ``database``
                    # is not in the closed v0.2 ``_GRAPH_NODE_KINDS``.
                    "to": {"name": "db", "kind": "volume"},
                },
                headers=_authed(token),
            )
            assert ann_resp.status_code == 201, ann_resp.text
            curated_id = uuid.UUID(ann_resp.json()["id"])

            # Both rows persist with bidirectional ``conflicts_with``.
            auto_props = await _get_edge_props(nodes["auto_edge"])
            curated_props = await _get_edge_props(curated_id)
            assert auto_props.get("conflicts_with") == [str(curated_id)], (
                "auto edge's conflicts_with should point at the curated row"
            )
            assert curated_props.get("conflicts_with") == [str(nodes["auto_edge"])], (
                "curated edge's conflicts_with should point at the auto row"
            )

            # GET /edges?conflicts=true returns exactly the two rows.
            list_resp = await client.get(
                "/api/v1/topology/edges?conflicts=true",
                headers=_authed(token),
            )
            assert list_resp.status_code == 200, list_resp.text
            ids = {row["id"] for row in list_resp.json()}
            assert ids == {str(nodes["auto_edge"]), str(curated_id)}


# ---------------------------------------------------------------------------
# §11 Tenant boundary
# ---------------------------------------------------------------------------


@_skip_no_docker
async def test_tenant_boundary_isolates_resolve_node_and_list_edges(
    annotate_app: FastAPI,
) -> None:
    """Tenant A cannot annotate a tenant-B-only node; list-edges is filtered.

    Two assertions on the §11 boundary:

    1. **resolve_node 404.** Seed a node named ``tenant-b-only`` in
       tenant B only. Tenant A's annotate referencing that name 404s
       at endpoint resolution — never resolves to the tenant-B node.
    2. **list-edges isolation.** Seed an auto edge in tenant B.
       Tenant A's ``GET /edges`` with no filter never returns a
       tenant-B row; the ``kind`` and ``source`` filters also stay
       isolated.
    """
    from tests._oidc_jwt_helpers import mock_discovery_and_jwks, public_jwks

    # --- Tenant A: one node so the from-endpoint resolves ---
    await _seed_node(tenant_id=TENANT_A_ID, kind="vm", name="vm-A")

    # --- Tenant B: one shared-named node (target) + one auto edge ---
    b_from = await _seed_node(tenant_id=TENANT_B_ID, kind="vm", name="tenant-b-only")
    b_to = await _seed_node(tenant_id=TENANT_B_ID, kind="host", name="b-host")
    await _seed_auto_edge(
        tenant_id=TENANT_B_ID,
        from_id=b_from,
        to_id=b_to,
        kind="runs-on",
    )

    key_a, token_a = _token(role=TenantRole.TENANT_ADMIN, tenant_id=TENANT_A_ID, sub="op-a")

    with patch(_PUBLISH_PATCH, new_callable=AsyncMock), respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key_a))
        async with _make_async_client(annotate_app) as client:
            # 1. Cross-tenant annotate — to-endpoint resolves to a tenant-B
            #    name; tenant-A's resolve_node must not see it. The boundary
            #    surfaces as 404 ``node_not_found`` (same shape every
            #    topology route uses for a missing graph node).
            ann_resp = await client.post(
                "/api/v1/topology/edges",
                json={
                    "from": {"name": "vm-A", "kind": "vm"},
                    "kind": "runs-on",
                    "to": {"name": "tenant-b-only", "kind": "vm"},
                },
                headers=_authed(token_a),
            )
            assert ann_resp.status_code == 404, ann_resp.text
            assert ann_resp.json()["detail"]["error"] == "node_not_found"

            # 2. Tenant A's list-edges sees zero rows — the only edge in
            #    the DB is the tenant-B auto edge above. Cross-filter
            #    combinations (kind, source) also stay empty.
            for path in (
                "/api/v1/topology/edges",
                "/api/v1/topology/edges?kind=runs-on",
                "/api/v1/topology/edges?source=auto",
            ):
                resp = await client.get(path, headers=_authed(token_a))
                assert resp.status_code == 200, resp.text
                assert resp.json() == [], (
                    f"tenant A's {path} leaked a tenant-B row: {resp.json()!r}"
                )

    # Sanity: the tenant-B edge actually exists in PG (so the empty list
    # above is the tenant filter, not an empty table).
    sm = get_sessionmaker()
    async with sm() as session:
        b_count = await session.execute(
            select(func.count())
            .select_from(GraphEdge)
            .where(GraphEdge.tenant_id == uuid.UUID(TENANT_B_ID))
        )
        assert int(b_count.scalar_one()) == 1


# ---------------------------------------------------------------------------
# §3 Unannotate safety — auto rows → 409, curated rows → 204 + cleanup
# ---------------------------------------------------------------------------


@_skip_no_docker
async def test_unannotate_safety_auto_edge_returns_409_curated_clears_markers(
    annotate_app: FastAPI,
) -> None:
    """Unannotating an auto edge 409s; unannotating a curated one cleans up.

    Two scenarios in one test — the asserts target distinct rows so
    a failure is unambiguous, satisfying the suite's
    "independently-failing tests" acceptance criterion when read
    against the §3 invariant as a single behaviour.
    """
    from tests._oidc_jwt_helpers import mock_discovery_and_jwks, public_jwks

    nodes = await _seed_same_kind_conflict_fixture(TENANT_A_ID)

    key, token = _token(role=TenantRole.TENANT_ADMIN, tenant_id=TENANT_A_ID, sub="op-a")

    with patch(_PUBLISH_PATCH, new_callable=AsyncMock), respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        async with _make_async_client(annotate_app) as client:
            # 1. DELETE on the seeded auto edge → 409 with the rule message.
            del_auto = await client.delete(
                f"/api/v1/topology/edges/{nodes['auto_edge']}",
                headers=_authed(token),
            )
            assert del_auto.status_code == 409, del_auto.text
            body = del_auto.json()
            assert body["detail"]["error"] == "auto_edge_deletion"
            # The route's rule message names the source axis explicitly so
            # the CLI / MCP fronts can surface the remediation prose.
            assert "source='auto'" in body["detail"]["message"]

            # 2. Annotate a curated row → DELETE it → 204; check that any
            #    reciprocal markers it left on the auto edge are cleared.
            ann_resp = await client.post(
                "/api/v1/topology/edges",
                json={
                    "from": {"name": "vm-A", "kind": "vm"},
                    "kind": "runs-on",
                    "to": {"name": "host-Y", "kind": "host"},
                },
                headers=_authed(token),
            )
            assert ann_resp.status_code == 201
            curated_id = uuid.UUID(ann_resp.json()["id"])

            # Pre-condition: the auto row picked up the marker.
            pre_props = await _get_edge_props(nodes["auto_edge"])
            assert pre_props.get("superseded_by") == str(curated_id)

            del_curated = await client.delete(
                f"/api/v1/topology/edges/{curated_id}",
                headers=_authed(token),
            )
            assert del_curated.status_code == 204

            # Post-condition: the auto row's marker is gone; the curated
            # row no longer resolves.
            post_props = await _get_edge_props(nodes["auto_edge"])
            assert "superseded_by" not in post_props

            sm = get_sessionmaker()
            async with sm() as session:
                gone = await session.get(GraphEdge, curated_id)
                assert gone is None


# ---------------------------------------------------------------------------
# §5 Role gating
# ---------------------------------------------------------------------------


@_skip_no_docker
async def test_role_gating_operator_post_delete_forbidden_read_only_get_forbidden(
    annotate_app: FastAPI,
) -> None:
    """The RBAC matrix from §5 of Initiative #364.

    * ``operator`` — POST / DELETE return 403.
    * ``read_only`` — GET returns 403 (the gate is ``OPERATOR``
      minimum on the read route; ``READ_ONLY`` is below that).
    * ``tenant_admin`` — POST / DELETE / GET all succeed.

    The same write surface is hit three times so the assertion fires
    on the role gate alone, not on a payload-shape divergence.
    """
    from tests._oidc_jwt_helpers import mock_discovery_and_jwks, public_jwks

    # Seed the endpoints so the tenant_admin POST has something to
    # resolve; operator / read_only failures land at the RBAC gate
    # before any DB read.
    await _seed_node(tenant_id=TENANT_A_ID, kind="vm", name="rg-vm")
    await _seed_node(tenant_id=TENANT_A_ID, kind="host", name="rg-host")

    key_op, token_op = _token(role=TenantRole.OPERATOR, tenant_id=TENANT_A_ID, sub="op-1")
    key_ro, token_ro = _token(role=TenantRole.READ_ONLY, tenant_id=TENANT_A_ID, sub="ro-1")
    key_ad, token_ad = _token(role=TenantRole.TENANT_ADMIN, tenant_id=TENANT_A_ID, sub="ad-1")

    annotate_body = {
        "from": {"name": "rg-vm", "kind": "vm"},
        "kind": "runs-on",
        "to": {"name": "rg-host", "kind": "host"},
    }

    with patch(_PUBLISH_PATCH, new_callable=AsyncMock), respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key_op, key_ro, key_ad))
        async with _make_async_client(annotate_app) as client:
            # operator: POST → 403, DELETE → 403
            op_post = await client.post(
                "/api/v1/topology/edges",
                json=annotate_body,
                headers=_authed(token_op),
            )
            assert op_post.status_code == 403, op_post.text

            op_delete = await client.delete(
                f"/api/v1/topology/edges/{uuid.uuid4()}",
                headers=_authed(token_op),
            )
            assert op_delete.status_code == 403, op_delete.text

            # operator GET stays at 200 — the read route is at OPERATOR.
            op_get = await client.get(
                "/api/v1/topology/edges",
                headers=_authed(token_op),
            )
            assert op_get.status_code == 200, op_get.text

            # read_only: GET → 403 (below OPERATOR).
            ro_get = await client.get(
                "/api/v1/topology/edges",
                headers=_authed(token_ro),
            )
            assert ro_get.status_code == 403, ro_get.text

            # tenant_admin: POST → 201; DELETE on the resulting id → 204;
            # GET → 200.
            ad_post = await client.post(
                "/api/v1/topology/edges",
                json=annotate_body,
                headers=_authed(token_ad),
            )
            assert ad_post.status_code == 201, ad_post.text
            curated_id = uuid.UUID(ad_post.json()["id"])

            ad_delete = await client.delete(
                f"/api/v1/topology/edges/{curated_id}",
                headers=_authed(token_ad),
            )
            assert ad_delete.status_code == 204, ad_delete.text

            ad_get = await client.get(
                "/api/v1/topology/edges",
                headers=_authed(token_ad),
            )
            assert ad_get.status_code == 200, ad_get.text


# ---------------------------------------------------------------------------
# §10 Audit / broadcast — one row + one event per write
# ---------------------------------------------------------------------------


@_skip_no_docker
async def test_audit_and_broadcast_one_per_write(
    annotate_app: FastAPI,
) -> None:
    """Each annotate / unannotate writes exactly one audit + one broadcast.

    The route handler binds ``audit_op_id`` /
    ``audit_op_class='write'`` via ``bind_contextvars`` (§10 of
    Initiative #364) so the chassis audit middleware attributes the
    HTTP-level row with the canonical id rather than the broadcast
    classifier's ``other`` fallback.

    The substrate also writes a *service-level* audit row inside the
    annotate / unannotate transactions (``method='topology.annotate'``
    / ``method='topology.unannotate'``). That second row is the one
    consumed by the broadcast classifier — counting both rows would
    overstate the "one per write" invariant, so the assertion below
    filters on the service-level rows by their canonical method.
    """
    from tests._oidc_jwt_helpers import mock_discovery_and_jwks, public_jwks

    await _seed_node(tenant_id=TENANT_A_ID, kind="vm", name="au-vm")
    await _seed_node(tenant_id=TENANT_A_ID, kind="host", name="au-host")

    key, token = _token(role=TenantRole.TENANT_ADMIN, tenant_id=TENANT_A_ID, sub="op-a")

    publish_mock = AsyncMock()
    with patch(_PUBLISH_PATCH, new=publish_mock), respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        async with _make_async_client(annotate_app) as client:
            # --- annotate ---
            ann = await client.post(
                "/api/v1/topology/edges",
                json={
                    "from": {"name": "au-vm", "kind": "vm"},
                    "kind": "runs-on",
                    "to": {"name": "au-host", "kind": "host"},
                },
                headers=_authed(token),
            )
            assert ann.status_code == 201
            curated_id = uuid.UUID(ann.json()["id"])

            # --- unannotate ---
            un = await client.delete(
                f"/api/v1/topology/edges/{curated_id}",
                headers=_authed(token),
            )
            assert un.status_code == 204

    # Service-level audit rows (one per write).
    ann_rows = await _service_audit_rows("topology.annotate", TENANT_A_ID)
    un_rows = await _service_audit_rows("topology.unannotate", TENANT_A_ID)
    assert len(ann_rows) == 1
    assert len(un_rows) == 1

    # Each service row's payload carries op_id + op_class='write'.
    assert ann_rows[0].payload["op_id"] == "topology.annotate"
    assert ann_rows[0].payload["op_class"] == "write"
    assert un_rows[0].payload["op_id"] == "topology.unannotate"
    assert un_rows[0].payload["op_class"] == "write"

    # The annotate row's target_id is populated iff the from-node is a
    # registered target; in this fixture ``au-vm`` is a plain vm node,
    # so target_id is None (§10 of Initiative #364).
    assert ann_rows[0].target_id is None

    # Broadcast was called exactly twice (one annotate + one unannotate).
    assert publish_mock.await_count == 2


# ---------------------------------------------------------------------------
# Collection-time smoke — runs even on no-Docker sandboxes
# ---------------------------------------------------------------------------


def test_module_imports_cleanly() -> None:
    """Cheap collection-time guard that runs on no-Docker sandboxes.

    Mirrors the same shape :mod:`tests.integration.test_topology_query`
    keeps: if a public symbol on the annotate / list_edges surface were
    renamed or removed, this fails first on every sandbox — not only
    the Docker-gated runners. The full behavioural matrix lives in
    the ``@_skip_no_docker``-guarded tests above.
    """
    from meho_backplane.topology import (
        AutoEdgeDeletionError,
        InvalidEdgeKindError,
        NodeRef,
        UnannotateSelectorError,
        annotate_edge,
        list_edges,
        unannotate_edge,
    )

    assert callable(annotate_edge)
    assert callable(unannotate_edge)
    assert callable(list_edges)
    # The four typed errors keep their ValueError base for the route layer's
    # blanket-except shape (mapped to 422 / 409 / 404 in api/v1/topology.py).
    for exc in (
        AutoEdgeDeletionError,
        InvalidEdgeKindError,
        UnannotateSelectorError,
    ):
        assert issubclass(exc, ValueError)
    # NodeRef is a frozen dataclass — kind defaults to None for bare-name lookup.
    ref = NodeRef(name="x")
    assert ref.kind is None
