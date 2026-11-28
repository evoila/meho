# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The delete of a hand-made topology node carries ``hard_to_undo="deletes"``.

The topology connector is the only way into MEHO's own graph store. So the
mark goes on when no call brings the deleted state back. The first tests
show that this holds for ``topology.delete_node``: after the delete, a new
node with the same kind and name gets a new id, and the edges of the old
node do not come back. Without the delete, a new annotate of the same edge
brings the old edge row back.

The other tests show the stored row and the call answers. With the switch
``CUSTOM_POLICY`` on, a service login's call under a standing grant waits
for a person. With the switch off it runs, as on main. A person's call
answers the same with the switch off and on.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import or_, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import (
    ApprovalRequest,
    GraphEdge,
    GraphNode,
    ServicePrincipalGrant,
    Tenant,
)
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.operations.typed_register import clear_typed_op_snapshot
from meho_backplane.settings import get_settings
from meho_backplane.topology import create_or_get_node, delete_node
from meho_backplane.topology.annotate import NodeRef, annotate_edge, unannotate_edge
from meho_backplane.topology.node_delete import NodeHasLiveEdgesError
from tests.approval_table import register_all_descriptors

_OP_ID = "topology.delete_node"
_CONNECTOR_ID = "topology-graph-1.x"
_SERVICE_SUB = "svc:topology-delete-mark"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("SERVICE_GRANT_DELETE_SHAPED_PATTERNS", raising=False)
    get_settings.cache_clear()
    clear_typed_op_snapshot()
    yield
    clear_typed_op_snapshot()
    get_settings.cache_clear()


@pytest.fixture
def _foreign_keys_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Turn on the foreign keys of SQLite, so the delete drops the node's edge rows."""
    monkeypatch.setenv("MEHO_SQLITE_FOREIGN_KEYS", "1")
    reset_engine_for_testing()
    yield
    reset_engine_for_testing()


async def _tenant(slug: str = "example-tenant") -> uuid.UUID:
    async with get_sessionmaker()() as session:
        existing: uuid.UUID | None = await session.scalar(
            select(Tenant.id).where(Tenant.slug == slug)
        )
        if existing is not None:
            return existing
        tenant_id = uuid.uuid4()
        session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
        await session.commit()
    return tenant_id


def _admin(tenant_id: uuid.UUID) -> Operator:
    return Operator(
        sub="person-topology-delete-mark",
        name=None,
        email=None,
        raw_jwt="",
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


async def _node(tenant_id: uuid.UUID, kind: str, name: str) -> uuid.UUID:
    async with get_sessionmaker()() as session:
        result = await create_or_get_node(session, _admin(tenant_id), kind=kind, name=name)
    return result.node.id


async def _annotate(tenant_id: uuid.UUID, from_name: str, to_name: str) -> uuid.UUID:
    async with get_sessionmaker()() as session:
        edge = await annotate_edge(
            session, _admin(tenant_id), NodeRef(from_name), "depends-on", NodeRef(to_name)
        )
    return edge.id


async def _stale_edge(tenant_id: uuid.UUID, from_id: uuid.UUID, to_id: uuid.UUID) -> uuid.UUID:
    """An edge row of the node that is no longer live (``last_seen`` NULL)."""
    edge_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        session.add(
            GraphEdge(
                id=edge_id,
                tenant_id=tenant_id,
                from_node_id=from_id,
                to_node_id=to_id,
                kind="depends-on",
                source="curated",
                properties={"note": "kept from an earlier annotate"},
                discovered_by="person-topology-delete-mark",
                first_seen=datetime.now(UTC),
                last_seen=None,
            )
        )
        await session.commit()
    return edge_id


async def _edge_ids_of(tenant_id: uuid.UUID, node_id: uuid.UUID) -> list[uuid.UUID]:
    async with get_sessionmaker()() as session:
        rows = await session.scalars(
            select(GraphEdge.id).where(
                GraphEdge.tenant_id == tenant_id,
                or_(GraphEdge.from_node_id == node_id, GraphEdge.to_node_id == node_id),
            )
        )
        return list(rows)


# ---------------------------------------------------------------------------
# The reason for the mark
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("_foreign_keys_on")
async def test_after_the_delete_a_node_with_the_same_name_has_none_of_the_old_edges() -> None:
    tenant_id = await _tenant()
    old_id = await _node(tenant_id, "service", "svc-a")
    await _node(tenant_id, "database", "db-b")
    stale_id = await _stale_edge(tenant_id, old_id, await _node(tenant_id, "queue", "q-c"))
    live_id = await _annotate(tenant_id, "svc-a", "db-b")

    async with get_sessionmaker()() as session:
        with pytest.raises(NodeHasLiveEdgesError):
            await delete_node(session, _admin(tenant_id), node_id=old_id)
    async with get_sessionmaker()() as session:
        await unannotate_edge(session, _admin(tenant_id), edge_id=live_id)
    async with get_sessionmaker()() as session:
        await delete_node(session, _admin(tenant_id), node_id=old_id)

    new_id = await _node(tenant_id, "service", "svc-a")

    assert new_id != old_id
    assert await _edge_ids_of(tenant_id, new_id) == []
    async with get_sessionmaker()() as session:
        assert await session.get(GraphEdge, stale_id) is None
    # A new annotate of the same edge makes a new row; the old one stays gone.
    async with get_sessionmaker()() as session:
        edge = await annotate_edge(
            session, _admin(tenant_id), NodeRef("svc-a"), "depends-on", NodeRef("q-c")
        )
    assert edge.id != stale_id
    assert edge.properties.get("note") is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("_foreign_keys_on")
async def test_without_the_delete_a_new_annotate_brings_the_old_edge_row_back() -> None:
    tenant_id = await _tenant()
    node_id = await _node(tenant_id, "service", "svc-a")
    stale_id = await _stale_edge(tenant_id, node_id, await _node(tenant_id, "queue", "q-c"))

    async with get_sessionmaker()() as session:
        edge = await annotate_edge(
            session, _admin(tenant_id), NodeRef("svc-a"), "depends-on", NodeRef("q-c")
        )

    assert edge.id == stale_id
    assert edge.last_seen is not None


# ---------------------------------------------------------------------------
# The stored row and the call answers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_stored_row_carries_the_delete_mark() -> None:
    (row,) = [
        row
        for row in await register_all_descriptors()
        if row.tenant_id is None and row.op_id == _OP_ID
    ]
    assert row.hard_to_undo == "deletes"
    assert (row.safety_level, row.requires_approval) == ("caution", False)


@pytest.fixture
def _dispatch_caches() -> Iterator[None]:
    reset_dispatcher_caches()
    reset_handler_cache()
    yield
    reset_dispatcher_caches()
    reset_handler_cache()


def _caller(tenant_id: uuid.UUID, kind: PrincipalKind) -> Operator:
    return Operator(
        sub=_SERVICE_SUB if kind is PrincipalKind.SERVICE else "person-topology-delete-mark",
        name=None,
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=kind,
    )


async def _seed_grant(tenant_id: uuid.UUID) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            ServicePrincipalGrant(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                principal_sub=_SERVICE_SUB,
                op_id=_OP_ID,
                connector_id=_CONNECTOR_ID,
                target_id=None,
                reason="fixture: standing grant",
                created_by_sub="fixture-admin",
            )
        )
        await session.commit()


async def _call(
    monkeypatch: pytest.MonkeyPatch, *, switch_on: bool, kind: PrincipalKind
) -> tuple[dict[str, Any], bool, list[str]]:
    """One call of the delete; the answer, whether the node is still there, the waiting ops."""
    monkeypatch.setenv("CUSTOM_POLICY", "true" if switch_on else "false")
    get_settings.cache_clear()
    await register_all_descriptors()
    tenant_id = await _tenant()
    node_id = await _node(tenant_id, "service", "svc-a")
    if kind is PrincipalKind.SERVICE:
        await _seed_grant(tenant_id)

    answer = await call_operation(
        _caller(tenant_id, kind),
        {"connector_id": _CONNECTOR_ID, "op_id": _OP_ID, "params": {"node_id": str(node_id)}},
    )

    async with get_sessionmaker()() as session:
        still_there = await session.get(GraphNode, node_id) is not None
        waiting = list(await session.scalars(select(ApprovalRequest.op_id)))
    return answer, still_there, waiting


@pytest.mark.asyncio
@pytest.mark.usefixtures("_dispatch_caches")
async def test_switch_off_a_service_login_with_a_standing_grant_runs_the_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer, still_there, waiting = await _call(
        monkeypatch, switch_on=False, kind=PrincipalKind.SERVICE
    )

    assert answer["status"] == "ok", answer
    assert still_there is False
    assert waiting == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("_dispatch_caches")
async def test_switch_on_a_service_login_with_a_standing_grant_waits_for_a_person(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer, still_there, waiting = await _call(
        monkeypatch, switch_on=True, kind=PrincipalKind.SERVICE
    )

    assert answer["status"] == "awaiting_approval", answer
    assert answer["extras"]["error_code"] == "awaiting_approval", answer
    assert still_there is True
    assert waiting == [_OP_ID]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_dispatch_caches")
async def test_a_persons_call_answers_the_same_with_the_switch_off_and_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    off = await _call(monkeypatch, switch_on=False, kind=PrincipalKind.USER)
    async with get_sessionmaker()() as session:
        for request in await session.scalars(select(ApprovalRequest)):
            await session.delete(request)
        await session.commit()
    on = await _call(monkeypatch, switch_on=True, kind=PrincipalKind.USER)

    def _shape(result: tuple[dict[str, Any], bool, list[str]]) -> tuple[Any, ...]:
        answer, still_there, waiting = result
        return answer["status"], answer.get("extras", {}).get("error_code"), still_there, waiting

    assert _shape(off) == _shape(on)
    assert _shape(on) == ("awaiting_approval", "awaiting_approval", True, [_OP_ID])
