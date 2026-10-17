# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The atomic scope cut-over (#3749).

:func:`cut_over` applies one staged manifest in **one** PostgreSQL
transaction. PostgreSQL runs DDL inside a transaction, so either all of
the following commit together or none of them do:

1. Lock the ``topology_identity_state`` row (``FOR UPDATE``). Writers
   hold a shared lock on it, so the cut-over waits for running writers
   and new writers wait for the cut-over.
2. Check the manifest: it must be ``staged`` (every target answered, no
   unresolved edge) and every planned scope must be well formed.
3. ``LOCK TABLE graph_node, graph_edge IN EXCLUSIVE MODE`` (reads go on,
   writes wait), then fingerprint the graph again. A different
   ``snapshot_version`` means the graph changed after staging: refuse.
4. Check that every target the plan relies on is still live.
5. Drop the old unique key ``graph_node_tenant_kind_name_idx``.
6. Apply the node decisions, then the edge decisions, each with its
   history row and one audit row per tenant.
7. Make ``source_scope`` required and make
   ``graph_node_tenant_scope_kind_name_idx`` unique on
   ``(tenant_id, source_scope, kind, name)``.
8. Switch the mode to ``scoped`` and mark the manifest ``cut_over``.

Any refusal raises :class:`CutoverRefusedError` before step 5, so the old
graph stays exactly as it was and stays readable. A crash anywhere rolls
the whole transaction back the same way. Recovery is always forward: stage
again (or re-run the same manifest if the graph has not changed) and cut
over. Nothing here ever merges scoped rows back under the old key.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
import structlog
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    SOURCE_SCOPE_PATTERN,
    AuditEventClass,
    AuditLog,
    GraphEdge,
    GraphHistoryChangeKind,
    GraphNode,
    Target,
)
from meho_backplane.topology.history import (
    edge_snapshot,
    node_snapshot,
    record_edge_change,
    record_node_change,
)
from meho_backplane.topology.identity_mode import IDENTITY_STATE
from meho_backplane.topology.scope_repair.plan import (
    PlannedEdge,
    PlannedNode,
    RepairPlan,
    snapshot_version,
)
from meho_backplane.topology.scope_repair.stage import SCOPE_REPAIR_SUB
from meho_backplane.topology.scope_repair.store import (
    StoredManifest,
    load_graph,
    load_manifest,
    mark_manifest,
)

__all__ = ["CutoverRefusedError", "CutoverResult", "cut_over"]

_log = structlog.get_logger(__name__)

_OP_ID = "topology.scope_cutover"
_SCOPE_RE = re.compile(SOURCE_SCOPE_PATTERN)

# Fully literal DDL. Run only inside the cut-over transaction.
_LOCK_GRAPH_SQL = sa.text("LOCK TABLE graph_node, graph_edge IN EXCLUSIVE MODE")
_DROP_OLD_KEY_SQL = sa.text("DROP INDEX graph_node_tenant_kind_name_idx")
_SCOPE_NOT_NULL_SQL = sa.text("ALTER TABLE graph_node ALTER COLUMN source_scope SET NOT NULL")
_DROP_SCOPE_INDEX_SQL = sa.text("DROP INDEX graph_node_tenant_scope_kind_name_idx")
_CREATE_SCOPED_KEY_SQL = sa.text(
    "CREATE UNIQUE INDEX graph_node_tenant_scope_kind_name_idx "
    "ON graph_node (tenant_id, source_scope, kind, name)"
)

#: Refusals that are about this manifest. They mark it ``refused``; the
#: operator stages again. Other refusals leave the manifest as it is.
_MANIFEST_REFUSALS = frozenset(
    {"snapshot_changed", "target_unavailable", "malformed_scope", "unresolved_edge"}
)


class CutoverRefusedError(RuntimeError):
    """The cut-over did not start. The old graph is unchanged."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        suffix = f" ({detail})" if detail else ""
        super().__init__(f"topology scope cut-over refused: {reason}{suffix}")


class CutoverResult(BaseModel):
    """What the cut-over wrote, by decision."""

    model_config = ConfigDict(frozen=True)

    manifest_id: uuid.UUID
    nodes: dict[str, int]
    edges: dict[str, int]


def _check_plan(plan: RepairPlan) -> None:
    """Refuse a plan the cut-over must not apply."""
    for problem in plan.problems:
        reason = "target_unavailable" if problem.code == "target_unavailable" else "unresolved_edge"
        raise CutoverRefusedError(reason, problem.detail)
    unavailable = [t for t in plan.targets if not t.available]
    if unavailable:
        raise CutoverRefusedError("target_unavailable", str(unavailable[0].target_id))
    for node in plan.nodes:
        if not _SCOPE_RE.fullmatch(node.scope):
            raise CutoverRefusedError("malformed_scope", f"{node.kind}/{node.name}: {node.scope!r}")
        if node.action == "legacy" and node.scope != f"legacy:{node.node_id}":
            raise CutoverRefusedError("malformed_scope", f"{node.kind}/{node.name}: {node.scope!r}")


async def _check_targets_live(session: AsyncSession, plan: RepairPlan) -> None:
    """Refuse when a target the plan relies on was deleted after staging."""
    wanted = {t.target_id for t in plan.targets}
    if not wanted:
        return
    live = set(
        (
            await session.execute(
                sa.select(Target.id).where(Target.id.in_(wanted), Target.deleted_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    missing = sorted(wanted - live)
    if missing:
        raise CutoverRefusedError("target_unavailable", str(missing[0]))


async def _lock_and_load(session: AsyncSession, manifest_id: uuid.UUID) -> StoredManifest:
    """Steps 1--4: take the locks, check the manifest and the snapshot."""
    conn = await session.connection()
    if conn.dialect.name != "postgresql":
        raise CutoverRefusedError("postgresql_required")
    mode = (
        await session.execute(
            sa.select(IDENTITY_STATE.c.mode).where(IDENTITY_STATE.c.id == 1).with_for_update()
        )
    ).scalar_one_or_none()
    if mode != "legacy":
        raise CutoverRefusedError("already_scoped" if mode == "scoped" else "state_missing")
    manifest = await load_manifest(session, manifest_id, lock=True)
    if manifest is None:
        raise CutoverRefusedError("manifest_not_found", str(manifest_id))
    if manifest.status != "staged":
        raise CutoverRefusedError(f"manifest_{manifest.status}")
    _check_plan(manifest.plan)
    await session.execute(_LOCK_GRAPH_SQL)
    nodes, edges = await load_graph(session)
    if snapshot_version(nodes, edges) != manifest.snapshot_version:
        raise CutoverRefusedError("snapshot_changed")
    await _check_targets_live(session, manifest.plan)
    return manifest


def _write_audit_rows(
    session: AsyncSession, plan: RepairPlan, manifest_id: uuid.UUID, now: datetime
) -> dict[uuid.UUID, uuid.UUID]:
    """One audit row per tenant the cut-over touches; returns tenant -> audit id."""
    tenants = sorted({n.tenant_id for n in plan.nodes} | {e.tenant_id for e in plan.edges})
    audit_ids: dict[uuid.UUID, uuid.UUID] = {}
    for tenant_id in tenants:
        audit_ids[tenant_id] = uuid.uuid4()
        node_counts = Counter(n.action for n in plan.nodes if n.tenant_id == tenant_id)
        edge_counts = Counter(e.action for e in plan.edges if e.tenant_id == tenant_id)
        session.add(
            AuditLog(
                id=audit_ids[tenant_id],
                event_class=AuditEventClass.OPERATION_EXECUTION,
                occurred_at=now,
                operator_sub=SCOPE_REPAIR_SUB,
                tenant_id=tenant_id,
                target_id=None,
                method="CUTOVER",
                path=_OP_ID,
                status_code=200,
                request_id=None,
                duration_ms=Decimal("0"),
                payload={
                    "op_id": _OP_ID,
                    "op_class": "write",
                    "manifest_id": str(manifest_id),
                    "nodes": dict(node_counts),
                    "edges": dict(edge_counts),
                },
            )
        )
    return audit_ids


def _apply_existing_node(row: GraphNode, planned: PlannedNode, provenance: str) -> None:
    """Change one existing row in place for ``adopt`` / ``keep`` / ``legacy``."""
    row.source_scope = planned.scope
    row.source_scope_provenance = provenance
    if planned.action == "adopt":
        if row.source == "auto" and planned.target_id is not None:
            row.target_id = planned.target_id
        row.last_seen = planned.observed_at
    elif planned.action == "legacy" and row.source == "auto":
        row.last_seen = None


async def _apply_nodes(
    session: AsyncSession,
    plan: RepairPlan,
    audit_ids: dict[uuid.UUID, uuid.UUID],
    provenance: str,
    now: datetime,
) -> None:
    """Step 6, node half: change existing rows, insert new ones, write history."""
    existing_ids = [n.node_id for n in plan.nodes if n.action != "publish"]
    rows: dict[uuid.UUID, GraphNode] = {}
    if existing_ids:
        found = await session.execute(sa.select(GraphNode).where(GraphNode.id.in_(existing_ids)))
        rows = {r.id: r for r in found.scalars().all()}
    for planned in plan.nodes:
        audit_id = audit_ids[planned.tenant_id]
        if planned.action == "publish":
            node = GraphNode(
                id=planned.node_id,
                tenant_id=planned.tenant_id,
                kind=planned.kind,
                name=planned.name,
                target_id=planned.target_id,
                source="auto",
                properties={},
                discovered_by=planned.discovered_by or "unknown",
                first_seen=planned.observed_at or now,
                last_seen=planned.observed_at or now,
                source_scope=planned.scope,
                source_scope_provenance=provenance,
            )
            session.add(node)
            record_node_change(
                session,
                node_id=node.id,
                tenant_id=planned.tenant_id,
                change_kind=GraphHistoryChangeKind.CREATED,
                before=None,
                after=node_snapshot(node),
                audit_id=audit_id,
                valid_from=now,
            )
            continue
        row = rows[planned.node_id]
        before = node_snapshot(row)
        _apply_existing_node(row, planned, provenance)
        after = node_snapshot(row)
        if after != before:
            record_node_change(
                session,
                node_id=row.id,
                tenant_id=planned.tenant_id,
                change_kind=GraphHistoryChangeKind.UPDATED,
                before=before,
                after=after,
                audit_id=audit_id,
                valid_from=now,
            )


def _new_edge(planned: PlannedEdge, now: datetime) -> GraphEdge:
    return GraphEdge(
        id=planned.edge_id,
        tenant_id=planned.tenant_id,
        from_node_id=planned.from_node_id,
        to_node_id=planned.to_node_id,
        kind=planned.kind,
        source="auto",
        properties={},
        discovered_by=planned.discovered_by or "unknown",
        first_seen=planned.observed_at or now,
        last_seen=planned.observed_at or now,
    )


def _apply_existing_edge(row: GraphEdge, planned: PlannedEdge) -> None:
    """``adopt`` re-marks the edge as seen; ``quarantine`` retires a probe edge."""
    if planned.action == "adopt":
        row.last_seen = planned.observed_at
    elif row.source == "auto":
        row.last_seen = None


async def _apply_edges(
    session: AsyncSession,
    plan: RepairPlan,
    audit_ids: dict[uuid.UUID, uuid.UUID],
    now: datetime,
) -> None:
    """Step 6, edge half: insert, re-mark or retire edges, write history.

    A quarantined curated edge is left exactly as it is: it is operator
    data. Readers already skip it because one of its ends is a legacy
    row.
    """
    existing_ids = [e.edge_id for e in plan.edges if e.action != "publish"]
    rows: dict[uuid.UUID, GraphEdge] = {}
    if existing_ids:
        found = await session.execute(sa.select(GraphEdge).where(GraphEdge.id.in_(existing_ids)))
        rows = {r.id: r for r in found.scalars().all()}
    for planned in plan.edges:
        audit_id = audit_ids[planned.tenant_id]
        if planned.action == "publish":
            edge = _new_edge(planned, now)
            session.add(edge)
            record_edge_change(
                session,
                edge_id=edge.id,
                tenant_id=planned.tenant_id,
                change_kind=GraphHistoryChangeKind.CREATED,
                before=None,
                after=edge_snapshot(edge),
                audit_id=audit_id,
                valid_from=now,
            )
            continue
        row = rows[planned.edge_id]
        was_live = row.last_seen is not None
        before = edge_snapshot(row)
        _apply_existing_edge(row, planned)
        if was_live == (row.last_seen is not None):
            continue  # a heartbeat, not a change worth a history row
        record_edge_change(
            session,
            edge_id=row.id,
            tenant_id=planned.tenant_id,
            change_kind=(
                GraphHistoryChangeKind.REMOVED if was_live else GraphHistoryChangeKind.UPDATED
            ),
            before=before,
            after=None if was_live else edge_snapshot(row),
            audit_id=audit_id,
            valid_from=now,
        )


async def _finalise_schema(session: AsyncSession) -> None:
    """Step 7: ``source_scope`` required, scoped key unique."""
    await session.execute(_SCOPE_NOT_NULL_SQL)
    await session.execute(_DROP_SCOPE_INDEX_SQL)
    await session.execute(_CREATE_SCOPED_KEY_SQL)


async def _apply(session: AsyncSession, manifest: StoredManifest) -> CutoverResult:
    """Steps 5--8 inside the caller's transaction."""
    now = datetime.now(UTC)
    plan = manifest.plan
    provenance = f"scope-repair:{manifest.id}"
    await session.execute(_DROP_OLD_KEY_SQL)
    audit_ids = _write_audit_rows(session, plan, manifest.id, now)
    await session.flush()
    await _apply_nodes(session, plan, audit_ids, provenance, now)
    await session.flush()
    await _apply_edges(session, plan, audit_ids, now)
    await session.flush()
    await _finalise_schema(session)
    await session.execute(
        sa.update(IDENTITY_STATE)
        .where(IDENTITY_STATE.c.id == 1)
        .values(mode="scoped", manifest_id=manifest.id, changed_at=now)
    )
    await mark_manifest(session, manifest.id, status="cut_over")
    return CutoverResult(
        manifest_id=manifest.id,
        nodes=dict(Counter(n.action for n in plan.nodes)),
        edges=dict(Counter(e.action for e in plan.edges)),
    )


async def cut_over(manifest_id: uuid.UUID) -> CutoverResult:
    """Apply a staged manifest atomically. See the module docstring.

    Raises:
        CutoverRefusedError: a check failed; nothing changed. Refusals about
            the manifest itself (changed snapshot, unavailable target,
            malformed scope, unresolved edge) also mark it ``refused``.
    """
    sessionmaker = get_sessionmaker()
    try:
        async with sessionmaker() as session, session.begin():
            manifest = await _lock_and_load(session, manifest_id)
            result = await _apply(session, manifest)
    except CutoverRefusedError as exc:
        _log.warning(
            "topology_scope_cutover_refused",
            manifest_id=str(manifest_id),
            reason=exc.reason,
        )
        if exc.reason in _MANIFEST_REFUSALS:
            async with sessionmaker() as session, session.begin():
                await mark_manifest(
                    session, manifest_id, status="refused", refusal_reason=exc.reason
                )
        raise
    _log.info("topology_scope_cutover_applied", **_log_fields(result))
    return result


def _log_fields(result: CutoverResult) -> dict[str, Any]:
    return {
        "manifest_id": str(result.manifest_id),
        "nodes": result.nodes,
        "edges": result.edges,
    }
