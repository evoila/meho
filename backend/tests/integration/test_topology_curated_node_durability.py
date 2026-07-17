# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Curated-node durability under refresh, against real PostgreSQL (#2536).

The end-to-end acceptance path for Task #2536: an operator seeds a
node via :func:`~meho_backplane.topology.nodes.create_or_get_node`
(the MCP ``meho.topology.create_node`` substrate), then a connector
refresh whose snapshot **re-asserts the same** ``(kind, name)`` runs,
then one whose snapshot **drops** it:

* Re-observation is a heartbeat: the operator's ``note`` /
  ``evidence_url`` / ``seeded_*`` bag is intact, ``target_id`` stays
  NULL (no adoption), ``source`` stays ``'curated'``, and only
  ``last_seen`` moves.
* Absence is not a removal: the curated node is never soft-deleted by
  a refresh.

Pre-#2536, ``_update_existing_node`` overwrote the properties bag with
the probe's view, adopted the row onto the refreshing target, and the
follow-up refresh soft-deleted it — recoverable only through the
history tables inside the 90-day retention window.

Runs against the ``pgvector/pgvector:pg16`` container via the shared
``pg_engine`` conftest fixture (same shape as
:mod:`tests.integration.test_topology_concurrency`).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector
from meho_backplane.connectors.schemas import (
    CandidateHint,
    FingerprintResult,
    NodeHint,
    OperationResult,
    ProbeResult,
    TopologyHints,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import GraphNode
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.topology.nodes import create_or_get_node
from meho_backplane.topology.refresh import refresh_target_topology
from tests.integration.conftest import DOCKER_AVAILABLE, SKIP_REASON

# Match the tenant rows the ``pg_engine`` conftest fixture seeds.
TENANT_A_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")

_skip_no_docker = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)

_PRODUCT = "curated-durability-product"


class _SnapshotConnector(Connector):
    """Connector whose ``discover_topology`` returns a class-level snapshot.

    The test mutates :attr:`hints` between refreshes to drive the
    re-observation and absence passes (same shape the unit suite's
    ``_FakeConnector`` uses).
    """

    product = _PRODUCT

    hints: ClassVar[TopologyHints] = TopologyHints(discovered_at=datetime.now(UTC))

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:
        raise NotImplementedError

    async def discover_topology(self, target: Any) -> TopologyHints:
        return type(self).hints

    async def list_candidates(self, seed_target: Any | None = None) -> list[CandidateHint]:
        return []


def _operator(tenant_id: uuid.UUID) -> Operator:
    return Operator(
        sub="op-curator",
        name=None,
        email=None,
        raw_jwt="not-a-real-jwt",
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


@pytest.fixture
async def snapshot_target(pg_engine: None) -> AsyncIterator[TargetORM]:
    """Register the connector, insert one target row, yield it loaded."""
    clear_registry()
    _CONNECTOR_INSTANCE_CACHE.clear()
    register_connector(_PRODUCT, _SnapshotConnector)

    target_id = uuid.uuid4()
    now = datetime.now(UTC)
    sm = get_sessionmaker()
    async with sm() as session, session.begin():
        session.add(
            TargetORM(
                id=target_id,
                tenant_id=TENANT_A_ID,
                name=f"durability-target-{uuid.uuid4().hex[:8]}",
                product=_PRODUCT,
                host="10.0.0.2",
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
    async with sm() as session:
        target = (
            await session.execute(select(TargetORM).where(TargetORM.id == target_id))
        ).scalar_one()

    yield target

    clear_registry()
    _CONNECTOR_INSTANCE_CACHE.clear()


async def _load_node(name: str) -> GraphNode:
    sm = get_sessionmaker()
    async with sm() as session:
        return (
            await session.execute(
                select(GraphNode).where(
                    GraphNode.tenant_id == TENANT_A_ID,
                    GraphNode.name == name,
                )
            )
        ).scalar_one()


@_skip_no_docker
async def test_seeded_node_survives_refresh_and_absence(
    snapshot_target: TargetORM,
) -> None:
    """Seed → re-observing refresh → absent refresh; the curated row endures."""
    op = _operator(TENANT_A_ID)
    seed_name = f"seeded-vm-{uuid.uuid4().hex[:8]}"

    # 1. Manual seed with operator context (the MCP create_node path).
    sm = get_sessionmaker()
    async with sm() as session:
        seed_result = await create_or_get_node(
            session,
            op,
            kind="vm",
            name=seed_name,
            note="cross-system anchor for the vault trace",
            evidence_url="https://example.test/inventory#L7",
        )
    assert seed_result.was_created is True
    assert seed_result.node.source == "curated"
    seeded_last_seen = seed_result.node.last_seen
    assert seeded_last_seen is not None

    # 2. Refresh whose snapshot re-asserts the same (kind, name) with
    #    probe properties.
    _SnapshotConnector.hints = TopologyHints(
        discovered_at=datetime.now(UTC),
        nodes=(
            NodeHint(kind="vm", name=seed_name, properties={"power": "on"}),
            NodeHint(kind="host", name="host-1"),
        ),
        edges=(),
    )
    first = await refresh_target_topology(snapshot_target, op)
    assert first.added_nodes == 1  # host-1 only; the curated node is a heartbeat
    assert first.updated_nodes == 0
    assert first.removed_nodes == 0

    node = await _load_node(seed_name)
    assert node.source == "curated"
    assert node.target_id is None, "curated nodes are never adopted onto a target"
    assert node.properties["note"] == "cross-system anchor for the vault trace"
    assert node.properties["evidence_url"] == "https://example.test/inventory#L7"
    assert node.properties["seeded_by"] == "op-curator"
    assert "power" not in node.properties, "probe view must not overwrite the operator bag"
    assert node.last_seen is not None
    assert node.last_seen > seeded_last_seen, "re-observation must bump last_seen"

    # 3. Refresh whose snapshot dropped the node: no soft-delete.
    _SnapshotConnector.hints = TopologyHints(
        discovered_at=datetime.now(UTC),
        nodes=(NodeHint(kind="host", name="host-1"),),
        edges=(),
    )
    second = await refresh_target_topology(snapshot_target, op)
    assert second.removed_nodes == 0

    node = await _load_node(seed_name)
    assert node.last_seen is not None, "a refresh must never soft-delete a curated node"
    assert node.source == "curated"
    assert node.properties["note"] == "cross-system anchor for the vault trace"
