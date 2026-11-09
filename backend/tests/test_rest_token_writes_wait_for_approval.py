# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A person's token call on the REST target and graph writes waits for approval.

* **Waits** -- ``POST``, ``PATCH``, ``DELETE /api/v1/targets`` and the four
  graph writes answer 202 with an approval request id when a person calls
  them with a token. The request names the matching typed operation and
  its params. Nothing is written.
* **Runs after approval** -- a second person approves; the approved call
  writes exactly what the request held. ``PATCH`` and ``DELETE`` carry the
  id of the target, so a new name or alias does not move the change.
* **Checked before the wait** -- a failed write check (``POST``,
  ``PATCH``), an unknown
  target (``PATCH``) and a target that topology rows point at
  (``DELETE`` without ``force``) answer at once; no request is made.
* **Runs at once** -- a bulk dry run changes nothing and runs at once, and
  a person in the web interface writes at once.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.api.v1.targets import router as targets_router
from meho_backplane.api.v1.topology import router as topology_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.targets.ops import register_targets_registry_operations
from meho_backplane.connectors.targets.update import register_targets_update_operation
from meho_backplane.connectors.topology.ops import register_topology_graph_operations
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, GraphEdge, GraphNode, Tenant
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)

from ._oidc_jwt_helpers import DEFAULT_TENANT_ID, make_rsa_keypair, mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks
from ._rest_write_helpers import run_writes_at_once
from ._targets_helpers import (
    _admin_token,
    _empty_connector_registry,  # noqa: F401
    _insert_target,
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)

_TENANT_ID = uuid.UUID(DEFAULT_TENANT_ID)
_EXISTING = "existing-system"
_EXISTING_HOST = "existing-system.example"


@pytest_asyncio.fixture(autouse=True)
async def _registered_ops(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    request.getfixturevalue("_settings_env")
    embedder = AsyncMock()
    embedder.encode_one.return_value = [0.1] * 384
    embedder.encode.return_value = [[0.1] * 384]
    embedder.dimension = 384
    reset_dispatcher_caches()
    await register_targets_registry_operations(embedding_service=embedder)
    await register_targets_update_operation(embedding_service=embedder)
    await register_topology_graph_operations(embedding_service=embedder)
    yield
    reset_dispatcher_caches()


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(targets_router)
    app.include_router(topology_router)
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(_app())


@pytest.fixture
def console_client() -> Iterator[TestClient]:
    yield TestClient(run_writes_at_once(_app()))


_KEY = make_rsa_keypair("kid-A")


def _send(client: TestClient, method: str, path: str, body: Any = None) -> Any:
    headers = {"Authorization": f"Bearer {_admin_token(_KEY)}"}
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, _public_jwks(_KEY))
        return client.request(method, path, json=body, headers=headers)


def _approver() -> Operator:
    return Operator(
        sub="second-person",
        name=None,
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


async def _requests() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(select(ApprovalRequest))
        return list(rows.scalars().all())


async def _approve_and_run(request_id: str) -> Any:
    approver = _approver()
    async with get_sessionmaker()() as session:
        request = await approve_request(session, uuid.UUID(request_id), operator=approver)
        await session.commit()
    return await resume_dispatch_after_approval(operator=approver, request=request)


def _assert_waits(response: Any, op_id: str) -> str:
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "awaiting_approval"
    assert body["op_id"] == op_id
    assert body["approval_request_id"] in body["message"]
    return str(body["approval_request_id"])


async def _live_targets() -> dict[str, TargetORM]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(TargetORM).where(
                TargetORM.tenant_id == _TENANT_ID, TargetORM.deleted_at.is_(None)
            )
        )
        return {row.name: row for row in rows.scalars().all()}


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------


async def test_a_token_create_waits_and_runs_after_approval(client: TestClient) -> None:
    body = {"name": "new-system", "product": "ssh", "host": "new-system.example", "port": 2222}
    response = _send(client, "POST", "/api/v1/targets", body)

    request_id = _assert_waits(response, "targets.register")
    [request] = await _requests()
    assert request.op_id == "targets.register"
    assert request.params == body
    assert "new-system" not in await _live_targets()

    result = await _approve_and_run(request_id)
    assert result.status == "ok", result
    assert (await _live_targets())["new-system"].port == 2222


async def test_a_token_create_that_fails_a_check_answers_at_once(client: TestClient) -> None:
    # The Vault login keys need a platform admin; the caller is a tenant_admin.
    body = {
        "name": "new-system",
        "product": "ssh",
        "host": "new-system.example",
        "extras": {"vault_role": "some-role"},
    }
    response = _send(client, "POST", "/api/v1/targets", body)

    assert response.status_code == 403, response.text
    assert await _requests() == []
    assert "new-system" not in await _live_targets()


async def test_a_token_create_with_a_taken_name_answers_409_at_once(client: TestClient) -> None:
    await _insert_target(name=_EXISTING, host=_EXISTING_HOST)
    body = {"name": _EXISTING, "product": "ssh", "host": "other.example"}
    response = _send(client, "POST", "/api/v1/targets", body)

    assert response.status_code == 409, response.text
    assert "already exists" in response.json()["detail"]
    assert await _requests() == []


async def test_a_token_create_may_reuse_the_name_of_a_removed_target(client: TestClient) -> None:
    await _insert_target(name=_EXISTING, host=_EXISTING_HOST, deleted_at=datetime.now(UTC))
    body = {"name": _EXISTING, "product": "ssh", "host": "other.example"}
    response = _send(client, "POST", "/api/v1/targets", body)

    _assert_waits(response, "targets.register")


async def test_a_token_change_waits_and_runs_on_the_same_target_id(client: TestClient) -> None:
    row = await _insert_target(name=_EXISTING, host=_EXISTING_HOST, aliases=["old-alias"])
    response = _send(client, "PATCH", "/api/v1/targets/old-alias", {"host": "other.example"})

    request_id = _assert_waits(response, "targets.update")
    [request] = await _requests()
    assert request.params == {"target_id": str(row.id), "host": "other.example"}
    assert (await _live_targets())[_EXISTING].host == _EXISTING_HOST

    result = await _approve_and_run(request_id)
    assert result.status == "ok", result
    assert result.result["target_id"] == str(row.id)
    assert result.result["updated_fields"] == ["host"]
    assert (await _live_targets())[_EXISTING].host == "other.example"


async def test_a_token_change_of_an_unknown_target_answers_404(client: TestClient) -> None:
    response = _send(client, "PATCH", "/api/v1/targets/no-such-target", {"host": "x.example"})

    assert response.status_code == 404, response.text
    assert await _requests() == []


async def test_a_token_change_with_a_null_product_answers_422(client: TestClient) -> None:
    await _insert_target(name=_EXISTING, host=_EXISTING_HOST)
    response = _send(client, "PATCH", f"/api/v1/targets/{_EXISTING}", {"product": None})

    assert response.status_code == 422, response.text
    assert await _requests() == []


async def test_a_token_delete_waits_and_runs_after_approval(client: TestClient) -> None:
    row = await _insert_target(name=_EXISTING, host=_EXISTING_HOST)
    response = _send(client, "DELETE", f"/api/v1/targets/{_EXISTING}")

    request_id = _assert_waits(response, "targets.remove")
    [request] = await _requests()
    assert request.params == {"target_id": str(row.id), "force": False}
    assert _EXISTING in await _live_targets()

    result = await _approve_and_run(request_id)
    assert result.status == "ok", result
    assert _EXISTING not in await _live_targets()


async def test_a_token_delete_with_topology_rows_answers_409_at_once(client: TestClient) -> None:
    row = await _insert_target(name=_EXISTING, host=_EXISTING_HOST)
    async with get_sessionmaker()() as session, session.begin():
        session.add(Tenant(id=_TENANT_ID, slug="t-rest", name="Tenant"))
        await session.flush()
        session.add(_node("target", "node-a", target_id=row.id))

    response = _send(client, "DELETE", f"/api/v1/targets/{_EXISTING}")
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["kind"] == "target_has_references"
    assert await _requests() == []

    forced = _send(client, "DELETE", f"/api/v1/targets/{_EXISTING}?force=true")
    _assert_waits(forced, "targets.remove")
    [request] = await _requests()
    assert request.params == {"target_id": str(row.id), "force": True}


async def test_a_person_in_the_web_interface_writes_targets_at_once(
    console_client: TestClient,
) -> None:
    created = _send(
        console_client,
        "POST",
        "/api/v1/targets",
        {"name": "n", "product": "ssh", "host": "n.example"},
    )
    changed = _send(console_client, "PATCH", "/api/v1/targets/n", {"notes": "hello"})
    removed = _send(console_client, "DELETE", "/api/v1/targets/n")

    assert created.status_code == 201, created.text
    assert changed.status_code == 200, changed.text
    assert changed.json()["notes"] == "hello"
    assert removed.status_code == 204, removed.text
    assert await _requests() == []


# ---------------------------------------------------------------------------
# Topology
# ---------------------------------------------------------------------------


def _node(kind: str, name: str, *, target_id: uuid.UUID | None = None) -> GraphNode:
    return GraphNode(
        id=uuid.uuid4(),
        tenant_id=_TENANT_ID,
        kind=kind,
        name=name,
        target_id=target_id,
        properties={},
        discovered_by="test",
        first_seen=datetime.now(UTC),
        source="curated",
    )


async def _seed_graph() -> dict[str, uuid.UUID]:
    """Four nodes and one curated edge app -> db; ``lonely`` has no edge."""
    async with get_sessionmaker()() as session, session.begin():
        session.add(Tenant(id=_TENANT_ID, slug="t-graph", name="Graph Tenant"))
        await session.flush()
        app, db, cache, lonely = (
            _node("service", "app"),
            _node("database", "db"),
            _node("service", "cache"),
            _node("host", "lonely"),
        )
        session.add_all([app, db, cache, lonely])
        await session.flush()
        edge = GraphEdge(
            id=uuid.uuid4(),
            tenant_id=_TENANT_ID,
            from_node_id=app.id,
            to_node_id=db.id,
            kind="depends-on",
            properties={},
            discovered_by="test",
            first_seen=datetime.now(UTC),
            source="curated",
        )
        session.add(edge)
    return {"edge": edge.id, "lonely": lonely.id}


async def _graph() -> tuple[set[uuid.UUID], set[str]]:
    async with get_sessionmaker()() as session:
        edges = await session.execute(select(GraphEdge.id))
        nodes = await session.execute(select(GraphNode.name))
        return set(edges.scalars().all()), set(nodes.scalars().all())


_EDGE = {"from": {"name": "app"}, "kind": "depends-on", "to": {"name": "cache", "kind": "service"}}
_EDGE_PARAMS = {
    "from_name": "app",
    "kind": "depends-on",
    "to_name": "cache",
    "to_node_kind": "service",
}


def _graph_write(write: str, ids: dict[str, uuid.UUID]) -> tuple[str, str, Any, str, Any]:
    """Method, path, body, op id and expected params of one graph write."""
    if write == "annotate":
        return "POST", "/api/v1/topology/edges", _EDGE, "topology.annotate", _EDGE_PARAMS
    if write == "bulk":
        return (
            "POST",
            "/api/v1/topology/edges/bulk",
            {"edges": [_EDGE]},
            "topology.bulk_import",
            {"rows": [_EDGE_PARAMS]},
        )
    if write == "unannotate":
        edge_id = str(ids["edge"])
        return (
            "DELETE",
            f"/api/v1/topology/edges/{edge_id}",
            None,
            "topology.unannotate",
            {"edge_id": edge_id},
        )
    node_id = str(ids["lonely"])
    return (
        "DELETE",
        f"/api/v1/topology/nodes/{node_id}",
        None,
        "topology.delete_node",
        {"node_id": node_id},
    )


_EXPECTED_GRAPH = {
    "annotate": (2, {"app", "db", "cache", "lonely"}),
    "bulk": (2, {"app", "db", "cache", "lonely"}),
    "unannotate": (0, {"app", "db", "cache", "lonely"}),
    "delete_node": (1, {"app", "db", "cache"}),
}


@pytest.mark.parametrize("write", ["annotate", "bulk", "unannotate", "delete_node"])
async def test_a_token_graph_write_waits_and_runs_after_approval(
    client: TestClient, write: str
) -> None:
    ids = await _seed_graph()
    method, path, body, op_id, params = _graph_write(write, ids)
    response = _send(client, method, path, body)

    request_id = _assert_waits(response, op_id)
    [request] = await _requests()
    assert request.op_id == op_id
    assert request.params == params
    assert await _graph() == ({ids["edge"]}, {"app", "db", "cache", "lonely"})

    result = await _approve_and_run(request_id)
    assert result.status == "ok", result
    edges, nodes = await _graph()
    edge_count, node_names = _EXPECTED_GRAPH[write]
    assert (len(edges), nodes) == (edge_count, node_names)


async def test_a_token_bulk_dry_run_runs_at_once(client: TestClient) -> None:
    ids = await _seed_graph()
    response = _send(
        client, "POST", "/api/v1/topology/edges/bulk", {"edges": [_EDGE], "dry_run": True}
    )

    assert response.status_code == 200, response.text
    assert response.json()["dry_run"] is True
    assert await _requests() == []
    assert await _graph() == ({ids["edge"]}, {"app", "db", "cache", "lonely"})


async def test_a_person_in_the_web_interface_writes_the_graph_at_once(
    console_client: TestClient,
) -> None:
    ids = await _seed_graph()
    annotated = _send(console_client, "POST", "/api/v1/topology/edges", _EDGE)
    removed = _send(console_client, "DELETE", f"/api/v1/topology/nodes/{ids['lonely']}")

    assert annotated.status_code == 201, annotated.text
    assert removed.status_code == 204, removed.text
    assert await _requests() == []
    edges, nodes = await _graph()
    assert len(edges) == 2
    assert nodes == {"app", "db", "cache"}
