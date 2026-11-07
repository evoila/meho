# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The four REST graph writes of ``/api/v1/topology`` are for people only.

* **Machine logins** -- a service login with the ``tenant_admin`` role
  gets 403 ``human_principal_required`` on ``POST /edges``,
  ``POST /edges/bulk``, ``DELETE /edges/{edge_id}`` and
  ``DELETE /nodes/{node_id}``. The text names the ``topology.*``
  operations a machine login uses instead. An agent login gets 403
  ``insufficient_role`` (MEHO caps its role at ``operator``). Nothing is
  written in any case.
* **Reads** -- a service login still lists the edges.
* **People** -- a ``tenant_admin`` person (the CLI uses these routes)
  still writes and deletes edges and nodes.
* **Wrong role** -- a person with the ``operator`` role still gets
  ``insufficient_role``.

The services run for real against the test database, so the tests check
the rows. Only the edge list read is mocked: its query needs PostgreSQL.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.api.v1.topology import MACHINE_TOPOLOGY_WRITE_REMEDIATION
from meho_backplane.api.v1.topology import router as topology_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import GraphEdge, GraphNode, Tenant
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import (
    AUDIENCE,
    DEFAULT_TENANT_ID,
    ISSUER,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)

_TENANT_ID = uuid.UUID(DEFAULT_TENANT_ID)
_WRITES = ["POST /edges", "POST /edges/bulk", "DELETE /edges", "DELETE /nodes"]
_OPERATION_IDS = (
    "topology.annotate",
    "topology.unannotate",
    "topology.delete_node",
    "topology.bulk_import",
)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(topology_router)
    yield TestClient(app)


def _token(key: Any, *, kind: str | None, role: TenantRole) -> str:
    return mint_token(
        key,
        sub=f"{kind or 'user'}:graph-writer",
        tenant_role=role.value,
        tenant_id=DEFAULT_TENANT_ID,
        principal_kind=kind,
    )


async def _add_node(session: Any, *, kind: str, name: str) -> uuid.UUID:
    node_id = uuid.uuid4()
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
            source="curated",
        )
    )
    return node_id


async def _seed_graph() -> dict[str, uuid.UUID]:
    """A tenant, three nodes, one curated edge, and one node with no edges."""
    async with get_sessionmaker()() as session, session.begin():
        session.add(Tenant(id=_TENANT_ID, slug="graph-tenant", name="Graph Tenant"))
        app_id = await _add_node(session, kind="service", name="app")
        db_id = await _add_node(session, kind="database", name="db")
        await _add_node(session, kind="service", name="cache")
        lonely_id = await _add_node(session, kind="host", name="lonely")
        await session.flush()
        edge_id = uuid.uuid4()
        session.add(
            GraphEdge(
                id=edge_id,
                tenant_id=_TENANT_ID,
                from_node_id=app_id,
                to_node_id=db_id,
                kind="depends-on",
                properties={},
                discovered_by="test",
                first_seen=datetime.now(UTC),
                source="curated",
            )
        )
    return {"edge": edge_id, "lonely": lonely_id}


def _send(client: TestClient, write: str, token: str, ids: dict[str, uuid.UUID]) -> Any:
    headers = {"Authorization": f"Bearer {token}"}
    if write == "POST /edges":
        body = {"from": {"name": "app"}, "kind": "depends-on", "to": {"name": "cache"}}
        return client.post("/api/v1/topology/edges", json=body, headers=headers)
    if write == "POST /edges/bulk":
        rows = [{"from": {"name": "cache"}, "kind": "depends-on", "to": {"name": "db"}}]
        return client.post("/api/v1/topology/edges/bulk", json={"edges": rows}, headers=headers)
    if write == "DELETE /edges":
        return client.delete(f"/api/v1/topology/edges/{ids['edge']}", headers=headers)
    return client.delete(f"/api/v1/topology/nodes/{ids['lonely']}", headers=headers)


async def _graph() -> tuple[set[uuid.UUID], set[str]]:
    async with get_sessionmaker()() as session:
        edges = await session.execute(select(GraphEdge.id))
        nodes = await session.execute(select(GraphNode.name))
        return set(edges.scalars().all()), set(nodes.scalars().all())


async def _assert_nothing_written(ids: dict[str, uuid.UUID]) -> None:
    edges, nodes = await _graph()
    assert edges == {ids["edge"]}
    assert nodes == {"app", "db", "cache", "lonely"}


@pytest.mark.asyncio
@pytest.mark.parametrize("write", _WRITES)
async def test_a_service_login_with_tenant_admin_cannot_write_the_graph(
    client: TestClient, write: str
) -> None:
    ids = await _seed_graph()
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = _send(
            client, write, _token(key, kind="service", role=TenantRole.TENANT_ADMIN), ids
        )

    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert detail == f"human_principal_required: {MACHINE_TOPOLOGY_WRITE_REMEDIATION}"
    for op_id in _OPERATION_IDS:
        assert op_id in detail
    await _assert_nothing_written(ids)


@pytest.mark.asyncio
@pytest.mark.parametrize("write", _WRITES)
async def test_an_agent_login_cannot_write_the_graph(client: TestClient, write: str) -> None:
    ids = await _seed_graph()
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = _send(
            client, write, _token(key, kind="agent", role=TenantRole.TENANT_ADMIN), ids
        )

    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "insufficient_role"}
    await _assert_nothing_written(ids)


def test_a_service_login_still_lists_edges(client: TestClient) -> None:
    key = make_rsa_keypair("kid-A")
    token = _token(key, kind="service", role=TenantRole.TENANT_ADMIN)
    fake_list = AsyncMock(return_value=[])
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.list_edges", fake_list),
    ):
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/topology/edges", headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 200, response.text
    fake_list.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_person_writes_and_deletes_edges_and_nodes(client: TestClient) -> None:
    ids = await _seed_graph()
    key = make_rsa_keypair("kid-A")
    token = _token(key, kind=None, role=TenantRole.TENANT_ADMIN)
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        annotated = _send(client, "POST /edges", token, ids)
        imported = _send(client, "POST /edges/bulk", token, ids)
        unannotated = _send(client, "DELETE /edges", token, ids)
        node_deleted = _send(client, "DELETE /nodes", token, ids)

    assert annotated.status_code == 201, annotated.text
    assert imported.status_code == 200, imported.text
    assert imported.json()["created"] == 1
    assert unannotated.status_code == 204, unannotated.text
    assert node_deleted.status_code == 204, node_deleted.text
    edges, nodes = await _graph()
    assert len(edges) == 2
    assert ids["edge"] not in edges
    assert nodes == {"app", "db", "cache"}


@pytest.mark.parametrize("write", _WRITES)
def test_a_person_without_tenant_admin_gets_insufficient_role(
    client: TestClient, write: str
) -> None:
    ids = {"edge": uuid.uuid4(), "lonely": uuid.uuid4()}
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = _send(client, write, _token(key, kind=None, role=TenantRole.OPERATOR), ids)

    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "insufficient_role"}
