# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Database reads and writes for the scope repair (#3749).

* :func:`lock_graph_tables` blocks graph writes until the caller's
  transaction ends; :func:`load_graph` then reads every ``graph_node`` /
  ``graph_edge`` row as the plan's :class:`~.plan.OldNode` /
  :class:`~.plan.OldEdge`.
* :data:`MANIFEST` is the Core table for ``topology_scope_manifest``
  (migration ``0111``); :func:`save_manifest`, :func:`load_manifest` and
  :func:`mark_manifest` read and write it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import GraphEdge, GraphNode
from meho_backplane.topology.scope_repair.plan import OldEdge, OldNode, RepairPlan

__all__ = [
    "MANIFEST",
    "StoredManifest",
    "load_graph",
    "load_manifest",
    "lock_graph_tables",
    "mark_manifest",
    "save_manifest",
]

MANIFEST: Final = sa.Table(
    "topology_scope_manifest",
    sa.MetaData(),
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("snapshot_version", sa.Text(), nullable=False),
    sa.Column("body", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
    sa.Column("created_by", sa.Text(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("refusal_reason", sa.Text(), nullable=True),
)


@dataclass(frozen=True)
class StoredManifest:
    """One ``topology_scope_manifest`` row with its parsed plan."""

    id: uuid.UUID
    status: str
    snapshot_version: str
    plan: RepairPlan
    created_by: str
    created_at: datetime
    refusal_reason: str | None


# Fully literal. Reads go on; inserts, updates and deletes wait.
_LOCK_GRAPH_SQL = sa.text("LOCK TABLE graph_node, graph_edge IN EXCLUSIVE MODE")


async def lock_graph_tables(session: AsyncSession) -> None:
    """Pause graph writes until the caller's transaction ends (PostgreSQL).

    Every writer waits, whether or not it reads the identity mode. Reads
    keep working. SQLite has no such lock and no concurrent writers in
    this code base, so the call is a no-op there.
    """
    conn = await session.connection()
    if conn.dialect.name == "postgresql":
        await session.execute(_LOCK_GRAPH_SQL)


async def load_graph(session: AsyncSession) -> tuple[list[OldNode], list[OldEdge]]:
    """Read every node and edge, all tenants, in the plan's shape."""
    node_rows = await session.execute(
        sa.select(
            GraphNode.id,
            GraphNode.tenant_id,
            GraphNode.kind,
            GraphNode.name,
            GraphNode.source,
            GraphNode.source_scope,
            GraphNode.last_seen,
        )
    )
    nodes = [
        OldNode(
            id=r.id,
            tenant_id=r.tenant_id,
            kind=r.kind,
            name=r.name,
            source=r.source,
            source_scope=r.source_scope,
            live=r.last_seen is not None,
        )
        for r in node_rows
    ]
    edge_rows = await session.execute(
        sa.select(
            GraphEdge.id,
            GraphEdge.tenant_id,
            GraphEdge.from_node_id,
            GraphEdge.to_node_id,
            GraphEdge.kind,
            GraphEdge.source,
            GraphEdge.last_seen,
        )
    )
    edges = [
        OldEdge(
            id=r.id,
            tenant_id=r.tenant_id,
            from_node_id=r.from_node_id,
            to_node_id=r.to_node_id,
            kind=r.kind,
            source=r.source,
            live=r.last_seen is not None,
        )
        for r in edge_rows
    ]
    return nodes, edges


async def save_manifest(
    session: AsyncSession,
    *,
    plan: RepairPlan,
    snapshot: str,
    created_by: str,
) -> StoredManifest:
    """Insert a new manifest: ``staged`` when the plan is complete, else ``incomplete``."""
    stored = StoredManifest(
        id=uuid.uuid4(),
        status="staged" if plan.complete else "incomplete",
        snapshot_version=snapshot,
        plan=plan,
        created_by=created_by,
        created_at=datetime.now(UTC),
        refusal_reason=None,
    )
    await session.execute(
        sa.insert(MANIFEST).values(
            id=stored.id,
            status=stored.status,
            snapshot_version=snapshot,
            body=plan.model_dump(mode="json"),
            created_by=created_by,
            created_at=stored.created_at,
        )
    )
    return stored


async def load_manifest(
    session: AsyncSession, manifest_id: uuid.UUID, *, lock: bool = False
) -> StoredManifest | None:
    """Read one manifest; ``lock=True`` adds ``FOR UPDATE``."""
    stmt = sa.select(MANIFEST).where(MANIFEST.c.id == manifest_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        return None
    return StoredManifest(
        id=row.id,
        status=row.status,
        snapshot_version=row.snapshot_version,
        plan=RepairPlan.model_validate(row.body),
        created_by=row.created_by,
        created_at=row.created_at,
        refusal_reason=row.refusal_reason,
    )


async def mark_manifest(
    session: AsyncSession,
    manifest_id: uuid.UUID,
    *,
    status: str,
    refusal_reason: str | None = None,
) -> None:
    """Set a manifest's final status (``cut_over`` or ``refused``)."""
    await session.execute(
        sa.update(MANIFEST)
        .where(MANIFEST.c.id == manifest_id)
        .values(status=status, refusal_reason=refusal_reason, finished_at=datetime.now(UTC))
    )
