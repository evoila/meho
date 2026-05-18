# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the G9.2-T8 bulk-import service (#600).

Coverage matrix (Task #600 acceptance criteria):

* **Happy-path batch** — a 3-row file lands all 3 edges in one
  transaction; one audit row per edge; one broadcast event per edge.
* **Idempotency** — re-running the same batch is a per-row no-op
  (row count unchanged, no new edge ids, ``update`` action).
* **Dry-run** — no edge row is created, no audit row is written, no
  broadcast event is published; the per-row plan still surfaces
  create / update / conflict classifications.
* **Validation failure (kind)** — one bad ``kind`` rejects the
  entire batch (no partial apply); the error envelope carries every
  row's failure.
* **Validation failure (missing endpoint)** — same atomicity.
* **Conflict classification** — a row whose endpoint pair already has
  an auto edge of a different ``to`` (same kind / different endpoint)
  routes through ``conflict`` so the operator sees the §6
  recoverability marker pre-apply.

The tests run against ``sqlite+aiosqlite`` via the autouse
``_default_database_url`` fixture in :mod:`tests.conftest` (same
shape :mod:`tests.test_topology_annotate` uses).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, GraphEdge, GraphNode, Tenant
from meho_backplane.settings import get_settings
from meho_backplane.topology import (
    BulkImportRow,
    BulkImportValidationError,
    NodeRef,
    bulk_import_edges,
)

_PUBLISH = "meho_backplane.topology.bulk_import._publish"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


async def _seed_tenant(slug: str = "rdc-internal") -> uuid.UUID:
    sessionmaker = get_sessionmaker()
    tenant_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
        await session.commit()
    return tenant_id


async def _seed_node(
    tenant_id: uuid.UUID,
    *,
    kind: str,
    name: str,
) -> uuid.UUID:
    sessionmaker = get_sessionmaker()
    node_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            GraphNode(
                id=node_id,
                tenant_id=tenant_id,
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


async def _seed_auto_edge(
    tenant_id: uuid.UUID,
    *,
    from_id: uuid.UUID,
    to_id: uuid.UUID,
    kind: str,
    properties: dict[str, Any] | None = None,
) -> uuid.UUID:
    sessionmaker = get_sessionmaker()
    edge_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            GraphEdge(
                id=edge_id,
                tenant_id=tenant_id,
                from_node_id=from_id,
                to_node_id=to_id,
                kind=kind,
                source="auto",
                properties=properties or {},
                discovered_by="test",
                first_seen=datetime.now(UTC),
                last_seen=datetime.now(UTC),
            )
        )
        await session.commit()
    return edge_id


def _operator(tenant_id: uuid.UUID, sub: str = "op-bulk") -> Operator:
    return Operator(
        sub=sub,
        name="Bulk Op",
        email=None,
        raw_jwt="",
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_import_creates_all_rows_in_one_transaction() -> None:
    """A 3-row batch creates 3 edges + 3 audit rows + 3 broadcast events."""
    tenant_id = await _seed_tenant()
    await _seed_node(tenant_id, kind="principal", name="sa-a")
    await _seed_node(tenant_id, kind="vault-role", name="vr-a")
    await _seed_node(tenant_id, kind="service", name="svc-orders")
    await _seed_node(tenant_id, kind="service", name="db-orders")
    await _seed_node(tenant_id, kind="vm", name="vm-1")
    await _seed_node(tenant_id, kind="host", name="host-1")

    rows = [
        BulkImportRow(
            from_ref=NodeRef("sa-a", "principal"),
            kind="authenticates-via",
            to_ref=NodeRef("vr-a", "vault-role"),
            note="SA → VR",
        ),
        BulkImportRow(
            from_ref=NodeRef("svc-orders", "service"),
            kind="depends-on",
            to_ref=NodeRef("db-orders", "service"),
            evidence_url="https://inv/L1",
        ),
        BulkImportRow(
            from_ref=NodeRef("vm-1", "vm"),
            kind="runs-on",
            to_ref=NodeRef("host-1", "host"),
        ),
    ]

    sessionmaker = get_sessionmaker()
    publish = AsyncMock()
    with patch(_PUBLISH, new=publish):
        async with sessionmaker() as session:
            result = await bulk_import_edges(session, _operator(tenant_id), rows)

    assert result.dry_run is False
    assert result.created == 3
    assert result.updated == 0
    assert result.conflicts == 0
    assert {r.index for r in result.rows} == {0, 1, 2}
    assert all(r.action == "create" for r in result.rows)
    assert all(r.edge_id is not None for r in result.rows)

    async with sessionmaker() as session:
        edges = (
            (await session.execute(select(GraphEdge).where(GraphEdge.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
        audits = (
            (await session.execute(select(AuditLog).where(AuditLog.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
    assert len(edges) == 3
    assert {e.source for e in edges} == {"curated"}
    assert len(audits) == 3
    assert {a.path for a in audits} == {"topology.annotate"}
    # One broadcast event per row.
    assert publish.await_count == 3


@pytest.mark.asyncio
async def test_bulk_import_is_idempotent() -> None:
    """Re-running the same batch is a per-row no-op (row count unchanged)."""
    tenant_id = await _seed_tenant()
    await _seed_node(tenant_id, kind="vm", name="vm-1")
    await _seed_node(tenant_id, kind="host", name="host-1")
    await _seed_node(tenant_id, kind="service", name="svc-1")
    await _seed_node(tenant_id, kind="service", name="db-1")

    rows = [
        BulkImportRow(
            from_ref=NodeRef("vm-1", "vm"),
            kind="runs-on",
            to_ref=NodeRef("host-1", "host"),
        ),
        BulkImportRow(
            from_ref=NodeRef("svc-1", "service"),
            kind="depends-on",
            to_ref=NodeRef("db-1", "service"),
        ),
    ]

    sessionmaker = get_sessionmaker()
    with patch(_PUBLISH, new=AsyncMock()):
        async with sessionmaker() as session:
            first = await bulk_import_edges(session, _operator(tenant_id), rows)
        async with sessionmaker() as session:
            second = await bulk_import_edges(session, _operator(tenant_id), rows)

    assert first.created == 2
    assert second.created == 0
    assert second.updated == 2
    assert all(r.action == "update" for r in second.rows)

    async with sessionmaker() as session:
        edges = (
            (await session.execute(select(GraphEdge).where(GraphEdge.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
    assert len(edges) == 2
    # The second run reused the same edge ids (idempotent upsert path).
    first_ids = {r.edge_id for r in first.rows}
    second_ids = {r.edge_id for r in second.rows}
    assert first_ids == second_ids


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_import_dry_run_writes_nothing() -> None:
    """``--dry-run`` produces the plan and performs zero writes."""
    tenant_id = await _seed_tenant()
    await _seed_node(tenant_id, kind="principal", name="sa-a")
    await _seed_node(tenant_id, kind="vault-role", name="vr-a")

    rows = [
        BulkImportRow(
            from_ref=NodeRef("sa-a", "principal"),
            kind="authenticates-via",
            to_ref=NodeRef("vr-a", "vault-role"),
        ),
    ]

    sessionmaker = get_sessionmaker()
    publish = AsyncMock()
    with patch(_PUBLISH, new=publish):
        async with sessionmaker() as session:
            result = await bulk_import_edges(session, _operator(tenant_id), rows, dry_run=True)

    assert result.dry_run is True
    assert result.created == 1
    assert result.rows[0].action == "create"
    assert result.rows[0].edge_id is None  # No row created yet in dry-run.

    async with sessionmaker() as session:
        edges = (
            (await session.execute(select(GraphEdge).where(GraphEdge.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
        audits = (
            (await session.execute(select(AuditLog).where(AuditLog.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
    assert edges == []
    assert audits == []
    publish.assert_not_called()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_import_rejects_whole_batch_on_invalid_kind() -> None:
    """One bad ``kind`` aborts the entire batch — no partial apply."""
    tenant_id = await _seed_tenant()
    await _seed_node(tenant_id, kind="vm", name="vm-1")
    await _seed_node(tenant_id, kind="host", name="host-1")
    await _seed_node(tenant_id, kind="service", name="svc-1")
    await _seed_node(tenant_id, kind="service", name="db-1")

    rows = [
        BulkImportRow(
            from_ref=NodeRef("vm-1", "vm"),
            kind="runs-on",
            to_ref=NodeRef("host-1", "host"),
        ),
        BulkImportRow(
            from_ref=NodeRef("svc-1", "service"),
            kind="not-a-real-kind",
            to_ref=NodeRef("db-1", "service"),
        ),
    ]

    sessionmaker = get_sessionmaker()
    with patch(_PUBLISH, new=AsyncMock()):
        async with sessionmaker() as session:
            with pytest.raises(BulkImportValidationError) as exc_info:
                await bulk_import_edges(session, _operator(tenant_id), rows)

    assert len(exc_info.value.errors) == 1
    err = exc_info.value.errors[0]
    assert err.index == 1
    assert err.error == "invalid_kind"
    assert err.kind == "not-a-real-kind"

    async with sessionmaker() as session:
        edges = (
            (await session.execute(select(GraphEdge).where(GraphEdge.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
    assert edges == []  # Row 0 didn't apply either — atomicity contract.


@pytest.mark.asyncio
async def test_bulk_import_rejects_missing_endpoint() -> None:
    """A row whose endpoint doesn't exist in the tenant fails the batch."""
    tenant_id = await _seed_tenant()
    await _seed_node(tenant_id, kind="vm", name="vm-1")
    # No host-1.

    rows = [
        BulkImportRow(
            from_ref=NodeRef("vm-1", "vm"),
            kind="runs-on",
            to_ref=NodeRef("host-missing", "host"),
        ),
    ]

    sessionmaker = get_sessionmaker()
    with patch(_PUBLISH, new=AsyncMock()):
        async with sessionmaker() as session:
            with pytest.raises(BulkImportValidationError) as exc_info:
                await bulk_import_edges(session, _operator(tenant_id), rows)

    err = exc_info.value.errors[0]
    assert err.error == "node_not_found"
    assert err.name == "host-missing"
    assert err.kind == "host"


@pytest.mark.asyncio
async def test_bulk_import_collects_every_row_failure() -> None:
    """Validation failures are aggregated — operator sees all rows at once."""
    tenant_id = await _seed_tenant()
    # No nodes seeded — every row fails endpoint resolution.

    rows = [
        BulkImportRow(
            from_ref=NodeRef("a", "vm"),
            kind="runs-on",
            to_ref=NodeRef("b", "host"),
        ),
        BulkImportRow(
            from_ref=NodeRef("c", "vm"),
            kind="bad-kind",
            to_ref=NodeRef("d", "host"),
        ),
    ]
    sessionmaker = get_sessionmaker()
    with patch(_PUBLISH, new=AsyncMock()):
        async with sessionmaker() as session:
            with pytest.raises(BulkImportValidationError) as exc_info:
                await bulk_import_edges(session, _operator(tenant_id), rows)
    # Both rows surface in the error envelope.
    assert {e.index for e in exc_info.value.errors} == {0, 1}


@pytest.mark.asyncio
async def test_bulk_import_empty_rows_is_noop() -> None:
    """An empty rows list returns a zero-row result; no error."""
    tenant_id = await _seed_tenant()
    sessionmaker = get_sessionmaker()
    with patch(_PUBLISH, new=AsyncMock()):
        async with sessionmaker() as session:
            result = await bulk_import_edges(session, _operator(tenant_id), [])
    assert result.created == 0
    assert result.rows == []


# ---------------------------------------------------------------------------
# Conflict classification (§6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_import_flags_supersede_as_conflict() -> None:
    """A row that would supersede an existing auto edge classifies ``conflict``.

    The auto-discovery probe found ``runs-on(vm-1 → host-old)``; the
    operator's curated batch asserts ``runs-on(vm-1 → host-new)``.
    The §6 same-kind / different-endpoint rule fires: the auto edge
    gets ``superseded_by`` stamped, and the bulk-import plan
    classifies the asserting row as ``conflict`` so the operator sees
    the recoverability listing in the response.
    """
    tenant_id = await _seed_tenant()
    vm = await _seed_node(tenant_id, kind="vm", name="vm-1")
    host_old = await _seed_node(tenant_id, kind="host", name="host-old")
    await _seed_node(tenant_id, kind="host", name="host-new")
    auto_edge_id = await _seed_auto_edge(tenant_id, from_id=vm, to_id=host_old, kind="runs-on")

    rows = [
        BulkImportRow(
            from_ref=NodeRef("vm-1", "vm"),
            kind="runs-on",
            to_ref=NodeRef("host-new", "host"),
        ),
    ]
    sessionmaker = get_sessionmaker()
    with patch(_PUBLISH, new=AsyncMock()):
        async with sessionmaker() as session:
            result = await bulk_import_edges(session, _operator(tenant_id), rows)

    assert result.rows[0].action == "conflict"
    assert str(auto_edge_id) in result.rows[0].superseded
