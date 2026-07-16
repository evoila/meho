# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""On-demand + scheduled topology refresh — diff/apply reconciliation.

Initiative #363 (G9.1), Task #450 (T3). :func:`refresh_target_topology`
is the single write path into the topology graph:

1. Resolve the connector for ``target`` via the G0.6 resolver.
2. Call ``connector.discover_topology(target)`` for a
   :class:`~meho_backplane.connectors.schemas.TopologyHints` snapshot.
3. Reconcile the snapshot against the existing ``graph_node`` +
   ``graph_edge`` rows for ``(tenant_id, target_id)``:

   * INSERT rows present in the snapshot but not in the DB.
   * UPDATE ``last_seen`` (+ ``properties`` when they changed) for rows
     in both.
   * Soft-delete (``last_seen = NULL``) rows in the DB but absent from
     the snapshot.

   Nodes are keyed by ``(kind, name)`` (the ``graph_node`` natural key
   within a tenant); edges by ``(from_kind, from_name, to_kind,
   to_name, kind)``. Edge endpoints are mapped to ``graph_node.id`` via
   the node key map built during the node pass, so an edge whose
   endpoint the snapshot does not also carry as a node is skipped (a
   well-formed connector always emits both).

The entire reconcile runs in **one transaction**: any failure rolls the
whole thing back, so a mid-reconcile crash never leaves the graph in a
half-applied state. One synchronous ``audit_log`` row is written
(``op_id="topology.refresh"``, ``op_class="read"``) and one fail-open
broadcast event is published per refresh, mirroring the G0.6 dispatcher's
:mod:`meho_backplane.operations._audit` discipline. Broadcast carries
only aggregate counts — no per-resource detail — so the read-class PII
defaults hold without a redactor pass (Initiative #363 item 11).
"""

from __future__ import annotations

import inspect
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import false, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.broadcast import BroadcastEvent, publish_event
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.connectors.resolver import resolve_connector
from meho_backplane.connectors.schemas import EdgeHint, NodeHint, TopologyHints
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, GraphEdge, GraphHistoryChangeKind, GraphNode
from meho_backplane.operations._audit import resolve_broadcast_lineage
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
)
from meho_backplane.topology.history import (
    edge_snapshot,
    node_snapshot,
    record_edge_change,
    record_node_change,
)

__all__ = ["RefreshResult", "refresh_target_topology"]

_log = structlog.get_logger(__name__)

#: ``op_id`` written to ``audit_log.path`` + carried in the broadcast
#: event. ``op_class="read"`` is passed explicitly rather than derived
#: via :func:`~meho_backplane.broadcast.events.classify_op` because the
#: ``.refresh`` suffix is neither a read nor a write verb suffix and
#: would otherwise classify as ``other``.
_REFRESH_OP_ID = "topology.refresh"
_REFRESH_OP_CLASS = "read"

#: HTTP-shaped ``audit_log`` columns reuse the dispatcher's convention:
#: a non-HTTP write records ``method`` as a verb token and ``path`` as
#: the canonical op_id (see :mod:`meho_backplane.operations._audit`).
_AUDIT_METHOD = "REFRESH"


class RefreshResult(BaseModel):
    """Per-target reconcile outcome returned by :func:`refresh_target_topology`.

    Counts are disjoint per object class: a node is counted in exactly
    one of added / updated / removed (or none, when unchanged). The same
    holds for edges. ``duration_ms`` is wall-clock for the whole
    resolve + discover + reconcile + commit cycle.

    ``no_populator_for_product`` discriminates the two all-zero-count
    no-op classes (#2093): ``None`` means the resolved connector ships a
    topology populator (a ``discover_topology`` override) and the counts
    reflect a real reconcile — all-zero is "populator ran clean, nothing
    changed". A product slug means the connector inherits the base-class
    no-op default, so the refresh was a no-op **by coverage gap**, not by
    graph convergence; ``populated_products`` then lists the registered
    products that DO ship a populator so a consumer can identify the gap
    without reading meho source.
    """

    model_config = ConfigDict(frozen=True)

    target_id: uuid.UUID
    added_nodes: int
    added_edges: int
    updated_nodes: int
    updated_edges: int
    removed_nodes: int
    removed_edges: int
    duration_ms: float
    no_populator_for_product: str | None = Field(
        default=None,
        description=(
            "Set to the target's product slug when the resolved connector "
            "has no topology populator (inherits the base-class "
            "discover_topology no-op), so the all-zero counts are a "
            "coverage gap rather than a clean no-op. Null when a "
            "populator ran."
        ),
    )
    populated_products: tuple[str, ...] | None = Field(
        default=None,
        description=(
            "Registered products that DO ship a topology populator, "
            "sorted. Only present alongside no_populator_for_product so "
            "the consumer can see which products refresh meaningfully."
        ),
    )


def _node_key(kind: str, name: str) -> tuple[str, str]:
    """Natural key for a node within a tenant.

    ``graph_node`` is unique on ``(tenant_id, kind, name)`` — the
    ``graph_node_tenant_kind_name_idx`` index is **target-independent**.
    A node can therefore already exist in the tenant under a different
    ``target_id`` (discovered by another target) or under
    ``target_id IS NULL`` (a manually-annotated node — see
    :func:`meho_backplane.topology.resolvers.resolve_node`). The
    reconcile must key its upsert decision on this tenant-wide grain,
    not on the per-target scope, or a node the snapshot re-asserts
    collides with the existing row on the unique index.
    """
    return (kind, name)


def _edge_key(
    from_kind: str,
    from_name: str,
    to_kind: str,
    to_name: str,
    kind: str,
) -> tuple[str, str, str, str, str]:
    """Natural key for an edge — the endpoint node keys plus the edge kind."""
    return (from_kind, from_name, to_kind, to_name, kind)


def _properties_differ(current: Any, incoming: Any) -> bool:
    """Return ``True`` when the persisted ``properties`` differ from discovery.

    ``GraphNode.properties`` / ``GraphEdge.properties`` round-trip as a
    plain ``dict`` from the JSON column; the connector hands back a
    ``MappingProxyType`` (frozen NodeHint/EdgeHint). Compare as plain
    dicts so a proxy-vs-dict identity mismatch doesn't force a spurious
    UPDATE every refresh.
    """
    return dict(current) != dict(incoming)


#: Keys in ``graph_edge.properties`` reserved for §6 conflict-detection
#: markers written by :mod:`meho_backplane.topology.annotate`.
#: :func:`_reconcile_edges` must **preserve** these on the refresh
#: write path — overwriting them clears the sticky-supersede invariant
#: on the next probe and silently brings a superseded auto edge back
#: into traversal. Initiative #364 §6 (Task #595, G9.2-T3) locks this
#: invariant; the merge logic in :func:`_merge_edge_properties` below
#: implements it.
_RESERVED_MARKER_KEYS: tuple[str, ...] = ("superseded_by", "conflicts_with")


def _strip_reserved_markers(properties: Any) -> dict[str, Any]:
    """Return ``properties`` as a plain dict with §6 markers removed.

    Reserved markers (``superseded_by`` / ``conflicts_with``) are
    operator-only artefacts — they may only enter ``graph_edge.properties``
    through :func:`annotate_edge`. A connector that emits them in an
    :class:`~meho_backplane.connectors.schemas.EdgeHint` (whether through
    a bug, copy-paste from a different row, or — in the hostile case —
    deliberate forgery) would otherwise smuggle a supersede / conflict
    mark onto an auto row and bypass the §6 annotate-only invariant.
    Stripping at the refresh boundary is fail-closed: even a malformed
    or untrusted hint cannot create a pre-superseded auto edge.
    """
    return {k: v for k, v in dict(properties or {}).items() if k not in _RESERVED_MARKER_KEYS}


def _merge_edge_properties(
    current: Any,
    incoming: Any,
) -> dict[str, Any]:
    """Return sanitized ``incoming`` merged with the reserved markers from ``current``.

    A refresh hint owns the connector's view of an edge — everything
    *except* the conflict markers an operator's annotation may have
    stamped on the row. Wholesale overwriting ``edge.properties`` (the
    pre-#595 behaviour) erased those markers on the next probe, which
    broke the sticky-supersede invariant in §6 of Initiative #364:

    * a curated annotation marked an auto edge ``superseded_by``;
    * the next refresh re-saw the auto edge and overwrote
      ``properties`` from the hint;
    * the supersede mark vanished and the auto edge silently
      reappeared in traversal.

    The fix is a key-level merge: reserved keys (``superseded_by``,
    ``conflicts_with``) survive from ``current``; everything else is
    sourced from ``incoming``. The merge is also one-sided in the
    other direction — reserved markers in ``incoming`` are dropped
    via :func:`_strip_reserved_markers` so a buggy or hostile
    connector cannot inject a supersede mark from the probe path.
    Only :func:`annotate_edge` may set those keys; only
    :func:`unannotate_edge` of the curated row clears the supersede
    mark (Initiative #364 §6).
    """
    merged = _strip_reserved_markers(incoming)
    current_dict = dict(current or {})
    for key in _RESERVED_MARKER_KEYS:
        if key in current_dict:
            merged[key] = current_dict[key]
    return merged


async def _load_reconcile_candidate_nodes(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    target_id: uuid.UUID,
    nodes: tuple[NodeHint, ...],
) -> list[GraphNode]:
    """Load every node a node-reconcile pass might touch, by natural key.

    ``graph_node`` is unique on ``(tenant_id, kind, name)``
    (target-independent), so the upsert decision must consider rows
    under *any* ``target_id`` — a node discovered by this target may
    already exist because another target discovered it, or because an
    operator annotated it (``target_id IS NULL``). The query unions two
    row sets within the tenant:

    * rows whose ``(kind, name)`` is in the current snapshot — the
      upsert candidates (the set the old ``target_id``-only filter
      missed, which is the #673 collision: a re-INSERT of a row that
      already exists under a different / NULL ``target_id`` blew the
      unique index mid-reconcile under autoflush);
    * rows already owned by *this* target — needed for the soft-delete
      pass, which must stay scoped to this target so a refresh never
      soft-deletes another target's node or a manual annotation.
    """
    discovered_pairs = {(n.kind, n.name) for n in nodes}
    snapshot_predicate = (
        tuple_(GraphNode.kind, GraphNode.name).in_(sorted(discovered_pairs))
        if discovered_pairs
        else false()
    )
    return list(
        (
            await session.execute(
                select(GraphNode).where(
                    GraphNode.tenant_id == tenant_id,
                    or_(
                        snapshot_predicate,
                        GraphNode.target_id == target_id,
                    ),
                )
            )
        )
        .scalars()
        .all()
    )


def _insert_discovered_node(
    session: AsyncSession,
    *,
    hint: NodeHint,
    tenant_id: uuid.UUID,
    target_id: uuid.UUID,
    discovered_by: str,
    now: datetime,
    audit_id: uuid.UUID,
) -> uuid.UUID:
    """Insert one fresh :class:`GraphNode` + emit its ``created`` history row.

    Returns the new node's id so the orchestrator can populate both
    the live and all key maps.
    """
    new_id = uuid.uuid4()
    new_node = GraphNode(
        id=new_id,
        tenant_id=tenant_id,
        kind=hint.kind,
        name=hint.name,
        target_id=target_id,
        properties=dict(hint.properties),
        discovered_by=discovered_by,
        first_seen=now,
        last_seen=now,
    )
    session.add(new_node)
    # Same-transaction history row: a ``created`` change with
    # ``before=None`` and ``after=`` the post-insert projection. The
    # hint's properties are already what landed on the row, so the
    # snapshot captures the committed state.
    record_node_change(
        session,
        node_id=new_id,
        tenant_id=tenant_id,
        change_kind=GraphHistoryChangeKind.CREATED,
        before=None,
        after=node_snapshot(new_node),
        audit_id=audit_id,
        valid_from=now,
    )
    return new_id


def _update_existing_node(
    session: AsyncSession,
    *,
    row: GraphNode,
    hint: NodeHint,
    tenant_id: uuid.UUID,
    target_id: uuid.UUID,
    now: datetime,
    audit_id: uuid.UUID,
) -> bool:
    """Update an existing :class:`GraphNode` row in place.

    Returns ``True`` when the row carried a meaningful change (the
    refresh ``updated`` counter increments + a history row fires).
    A pure ``last_seen`` heartbeat (no observable column change)
    returns ``False`` -- the row is touched but neither counted nor
    recorded.

    Adopts the row onto this target: a node first seen as a manual
    annotation (``target_id IS NULL``) or discovered by another
    target is now observed by *this* target's probe, so this target
    owns its lifecycle (and its future soft-delete) going forward.
    """
    is_meaningful_update = (
        row.last_seen is None
        or row.target_id != target_id
        or _properties_differ(row.properties, hint.properties)
    )
    # Capture the pre-mutation snapshot **before** reassigning the
    # row's columns -- otherwise the captured ``before`` aliases the
    # post-mutation state.
    before = node_snapshot(row) if is_meaningful_update else None
    row.properties = dict(hint.properties)
    row.target_id = target_id
    row.last_seen = now
    if is_meaningful_update:
        record_node_change(
            session,
            node_id=row.id,
            tenant_id=tenant_id,
            change_kind=GraphHistoryChangeKind.UPDATED,
            before=before,
            after=node_snapshot(row),
            audit_id=audit_id,
            valid_from=now,
        )
    return is_meaningful_update


def _soft_remove_node(
    session: AsyncSession,
    *,
    row: GraphNode,
    tenant_id: uuid.UUID,
    now: datetime,
    audit_id: uuid.UUID,
) -> None:
    """Null the row's ``last_seen`` and emit its ``removed`` history row.

    Capture the pre-removal snapshot **before** nulling
    ``last_seen`` so ``snapshot.before`` reflects the row as the
    operator last saw it (last_seen non-NULL); ``snapshot.after``
    is None per the ``removed`` change-kind contract.
    """
    before = node_snapshot(row)
    row.last_seen = None
    record_node_change(
        session,
        node_id=row.id,
        tenant_id=tenant_id,
        change_kind=GraphHistoryChangeKind.REMOVED,
        before=before,
        after=None,
        audit_id=audit_id,
        valid_from=now,
    )


async def _reconcile_nodes(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    target_id: uuid.UUID,
    discovered_by: str,
    nodes: tuple[NodeHint, ...],
    now: datetime,
    audit_id: uuid.UUID,
) -> tuple[
    int,
    int,
    int,
    dict[tuple[str, str], uuid.UUID],
    dict[tuple[str, str], uuid.UUID],
]:
    """Apply the node half of the diff. Returns counts + two key→id maps.

    * ``live_key_to_id`` — only nodes present in the current snapshot
      (inserted or kept). The edge pass resolves *discovered* edge
      endpoints through this map; an edge pointing at a node the
      current discovery dropped is itself dropped (soft-deleted).
    * ``all_key_to_id`` — every loaded node (see
      :func:`_load_reconcile_candidate_nodes`), including ones
      soft-deleted this refresh or on a prior one. The edge pass needs
      this to map an *existing* edge's ``from/to`` node ids back to keys
      so it can decide whether that edge is still discovered (and to
      soft-delete it when it is not).
    """
    existing_rows = await _load_reconcile_candidate_nodes(
        session, tenant_id=tenant_id, target_id=target_id, nodes=nodes
    )
    existing_by_key = {_node_key(r.kind, r.name): r for r in existing_rows}

    discovered_by_key: dict[tuple[str, str], NodeHint] = {
        _node_key(n.kind, n.name): n for n in nodes
    }

    added = updated = removed = 0
    live_key_to_id: dict[tuple[str, str], uuid.UUID] = {}
    all_key_to_id: dict[tuple[str, str], uuid.UUID] = {
        key: row.id for key, row in existing_by_key.items()
    }

    for key, hint in discovered_by_key.items():
        row = existing_by_key.get(key)
        if row is None:
            new_id = _insert_discovered_node(
                session,
                hint=hint,
                tenant_id=tenant_id,
                target_id=target_id,
                discovered_by=discovered_by,
                now=now,
                audit_id=audit_id,
            )
            live_key_to_id[key] = new_id
            all_key_to_id[key] = new_id
            added += 1
            continue
        if _update_existing_node(
            session,
            row=row,
            hint=hint,
            tenant_id=tenant_id,
            target_id=target_id,
            now=now,
            audit_id=audit_id,
        ):
            updated += 1
        live_key_to_id[key] = row.id

    for key, row in existing_by_key.items():
        if key in discovered_by_key:
            continue
        if row.target_id != target_id:
            # Owned by another target (or a manual annotation): not in
            # this target's snapshot is expected, not a removal. The
            # owning target's own refresh decides its fate.
            continue
        if row.last_seen is None:
            # Already soft-deleted on a prior refresh; not a fresh removal.
            continue
        _soft_remove_node(
            session,
            row=row,
            tenant_id=tenant_id,
            now=now,
            audit_id=audit_id,
        )
        removed += 1

    return added, updated, removed, live_key_to_id, all_key_to_id


_EdgeKey = tuple[str, str, str, str, str]


async def _load_existing_edges_by_key(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    all_node_key_to_id: dict[tuple[str, str], uuid.UUID],
) -> dict[_EdgeKey, GraphEdge]:
    """Index existing edges of the target by their natural key.

    Loaded by ``from_node_id`` over **every** node in the
    ``(tenant, target)`` scope (including nodes soft-deleted this
    refresh or earlier) so an edge whose endpoint was just dropped is
    still found and soft-deleted rather than silently orphaned.
    """
    all_node_ids = set(all_node_key_to_id.values())
    if not all_node_ids:
        return {}
    existing_rows = (
        (
            await session.execute(
                select(GraphEdge).where(
                    GraphEdge.tenant_id == tenant_id,
                    GraphEdge.from_node_id.in_(all_node_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    id_to_key = {v: k for k, v in all_node_key_to_id.items()}
    out: dict[_EdgeKey, GraphEdge] = {}
    for row in existing_rows:
        from_key = id_to_key.get(row.from_node_id)
        to_key = id_to_key.get(row.to_node_id)
        if from_key is None or to_key is None:
            continue
        out[_edge_key(from_key[0], from_key[1], to_key[0], to_key[1], row.kind)] = row
    return out


def _index_discovered_edges(
    edges: tuple[EdgeHint, ...],
    live_node_key_to_id: dict[tuple[str, str], uuid.UUID],
) -> dict[_EdgeKey, EdgeHint]:
    """Index snapshot edges whose endpoints exist in the live node map.

    An edge whose endpoint is not in the live snapshot (a malformed
    connector emitting an edge without its node) is logged and dropped
    rather than inserted as a dangling FK.
    """
    out: dict[_EdgeKey, EdgeHint] = {}
    for e in edges:
        from_id = live_node_key_to_id.get(_node_key(e.from_kind, e.from_name))
        to_id = live_node_key_to_id.get(_node_key(e.to_kind, e.to_name))
        if from_id is None or to_id is None:
            _log.warning(
                "topology_refresh_edge_dangling",
                from_kind=e.from_kind,
                from_name=e.from_name,
                to_kind=e.to_kind,
                to_name=e.to_name,
                edge_kind=e.kind,
            )
            continue
        out[_edge_key(e.from_kind, e.from_name, e.to_kind, e.to_name, e.kind)] = e
    return out


def _insert_discovered_edge(
    session: AsyncSession,
    *,
    hint: EdgeHint,
    from_id: uuid.UUID,
    to_id: uuid.UUID,
    tenant_id: uuid.UUID,
    discovered_by: str,
    now: datetime,
    audit_id: uuid.UUID,
) -> None:
    """Insert one fresh :class:`GraphEdge` + emit its ``created`` history row.

    Sanitize before insert so a connector emitting a reserved marker
    in its hint (buggy or hostile) cannot create a pre-superseded /
    pre-conflicting auto edge that bypasses the §6 annotate-only
    invariant (Initiative #364).
    """
    new_edge_id = uuid.uuid4()
    new_edge = GraphEdge(
        id=new_edge_id,
        tenant_id=tenant_id,
        from_node_id=from_id,
        to_node_id=to_id,
        kind=hint.kind,
        source="auto",
        properties=_strip_reserved_markers(hint.properties),
        discovered_by=discovered_by,
        first_seen=now,
        last_seen=now,
    )
    session.add(new_edge)
    record_edge_change(
        session,
        edge_id=new_edge_id,
        tenant_id=tenant_id,
        change_kind=GraphHistoryChangeKind.CREATED,
        before=None,
        after=edge_snapshot(new_edge),
        audit_id=audit_id,
        valid_from=now,
    )


def _refresh_curated_edge(
    session: AsyncSession,
    *,
    existing_edge: GraphEdge,
    tenant_id: uuid.UUID,
    now: datetime,
    audit_id: uuid.UUID,
) -> bool:
    """Bump ``last_seen`` on a curated edge; return ``True`` if it was a
    meaningful change.

    Curated edges are operator-owned: the probe's view of the row's
    properties is not authoritative. The refresh only records that
    the probe still observes the edge (``last_seen`` bump); the
    operator-supplied ``note`` / ``evidence_url`` / ``annotated_*``
    and any §6 markers stay untouched. Without this branch the next
    refresh after :func:`annotate_edge` of a previously-auto edge
    would wipe the caller's free-text props (Initiative #364 §6 — M1
    of PR #651 review).

    A *resurrected* curated row (was soft-deleted, now re-observed)
    is operator-observable and warrants an ``updated`` history row.
    A pure heartbeat (already-live curated edge re-seen) is not
    counted and no history row fires.
    """
    resurrected = existing_edge.last_seen is None
    if resurrected:
        before = edge_snapshot(existing_edge)
        existing_edge.last_seen = now
        record_edge_change(
            session,
            edge_id=existing_edge.id,
            tenant_id=tenant_id,
            change_kind=GraphHistoryChangeKind.UPDATED,
            before=before,
            after=edge_snapshot(existing_edge),
            audit_id=audit_id,
            valid_from=now,
        )
        return True
    existing_edge.last_seen = now
    return False


def _update_existing_auto_edge(
    session: AsyncSession,
    *,
    existing_edge: GraphEdge,
    hint: EdgeHint,
    tenant_id: uuid.UUID,
    now: datetime,
    audit_id: uuid.UUID,
) -> bool:
    """Merge an :class:`EdgeHint` onto an existing auto :class:`GraphEdge`.

    Returns ``True`` when the row carried a meaningful change.
    Reserved §6 conflict markers (``superseded_by`` /
    ``conflicts_with``) an operator's annotation may have stamped on
    this row survive the refresh -- the wholesale-overwrite this
    used to do silently cleared the sticky-supersede invariant; see
    :func:`_merge_edge_properties`.
    """
    merged_properties = _merge_edge_properties(existing_edge.properties, hint.properties)
    is_meaningful_update = existing_edge.last_seen is None or _properties_differ(
        existing_edge.properties, merged_properties
    )
    before_merge: dict[str, Any] | None = (
        edge_snapshot(existing_edge) if is_meaningful_update else None
    )
    existing_edge.properties = merged_properties
    existing_edge.last_seen = now
    if is_meaningful_update:
        record_edge_change(
            session,
            edge_id=existing_edge.id,
            tenant_id=tenant_id,
            change_kind=GraphHistoryChangeKind.UPDATED,
            before=before_merge,
            after=edge_snapshot(existing_edge),
            audit_id=audit_id,
            valid_from=now,
        )
    return is_meaningful_update


def _soft_remove_edge(
    session: AsyncSession,
    *,
    row: GraphEdge,
    tenant_id: uuid.UUID,
    now: datetime,
    audit_id: uuid.UUID,
) -> None:
    """Null the edge's ``last_seen`` and emit its ``removed`` history row."""
    before = edge_snapshot(row)
    row.last_seen = None
    record_edge_change(
        session,
        edge_id=row.id,
        tenant_id=tenant_id,
        change_kind=GraphHistoryChangeKind.REMOVED,
        before=before,
        after=None,
        audit_id=audit_id,
        valid_from=now,
    )


async def _reconcile_edges(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    discovered_by: str,
    edges: tuple[EdgeHint, ...],
    live_node_key_to_id: dict[tuple[str, str], uuid.UUID],
    all_node_key_to_id: dict[tuple[str, str], uuid.UUID],
    now: datetime,
    audit_id: uuid.UUID,
) -> tuple[int, int, int]:
    """Apply the edge half of the diff against the resolved node id maps.

    Discovered edge endpoints resolve through ``live_node_key_to_id``
    (only nodes in the current snapshot); an edge whose endpoint left
    the snapshot is treated as not-discovered and falls through to the
    soft-delete pass.
    """
    existing_by_key = await _load_existing_edges_by_key(
        session, tenant_id=tenant_id, all_node_key_to_id=all_node_key_to_id
    )
    discovered_by_key = _index_discovered_edges(edges, live_node_key_to_id)

    added = updated = removed = 0

    for key, hint in discovered_by_key.items():
        existing_edge = existing_by_key.get(key)
        from_id = live_node_key_to_id[_node_key(hint.from_kind, hint.from_name)]
        to_id = live_node_key_to_id[_node_key(hint.to_kind, hint.to_name)]
        if existing_edge is None:
            _insert_discovered_edge(
                session,
                hint=hint,
                from_id=from_id,
                to_id=to_id,
                tenant_id=tenant_id,
                discovered_by=discovered_by,
                now=now,
                audit_id=audit_id,
            )
            added += 1
            continue
        if existing_edge.source == "curated":
            if _refresh_curated_edge(
                session,
                existing_edge=existing_edge,
                tenant_id=tenant_id,
                now=now,
                audit_id=audit_id,
            ):
                updated += 1
            continue

        if _update_existing_auto_edge(
            session,
            existing_edge=existing_edge,
            hint=hint,
            tenant_id=tenant_id,
            now=now,
            audit_id=audit_id,
        ):
            updated += 1

    for key, row in existing_by_key.items():
        if key in discovered_by_key:
            continue
        if row.last_seen is None:
            continue
        _soft_remove_edge(
            session,
            row=row,
            tenant_id=tenant_id,
            now=now,
            audit_id=audit_id,
        )
        removed += 1

    return added, updated, removed


async def _write_audit_and_broadcast(
    *,
    session: AsyncSession,
    audit_id: uuid.UUID,
    operator: Operator,
    target_id: uuid.UUID,
    result: RefreshResult,
) -> None:
    """Write the audit row in the reconcile transaction; publish fail-open.

    The audit row is added to the **same** session as the reconcile so
    the spec's "no success without a committed audit row" invariant
    holds: if the audit insert fails the whole refresh rolls back. The
    broadcast publish is fail-open per :func:`publish_event`'s contract
    and is issued by the caller *after* the transaction commits.
    """
    counts = {
        "added_nodes": result.added_nodes,
        "added_edges": result.added_edges,
        "updated_nodes": result.updated_nodes,
        "updated_edges": result.updated_edges,
        "removed_nodes": result.removed_nodes,
        "removed_edges": result.removed_edges,
    }
    session.add(
        AuditLog(
            id=audit_id,
            occurred_at=datetime.now(UTC),
            operator_sub=operator.sub,
            tenant_id=operator.tenant_id,
            target_id=target_id,
            method=_AUDIT_METHOD,
            path=_REFRESH_OP_ID,
            status_code=200,
            request_id=None,
            duration_ms=Decimal(str(round(result.duration_ms, 2))),
            payload={
                "op_id": _REFRESH_OP_ID,
                "op_class": _REFRESH_OP_CLASS,
                "target_id": str(target_id),
                **counts,
            },
        )
    )


async def _publish_refresh_event(
    *,
    audit_id: uuid.UUID,
    operator: Operator,
    target_name: str,
    result: RefreshResult,
) -> None:
    """Emit the per-refresh broadcast event. Fail-open by contract.

    The payload is aggregate-only counts plus the op metadata — no node
    or edge names — so the read-class default holds without a redactor
    pass (Initiative #363 item 11).
    """
    lineage = resolve_broadcast_lineage()
    event = BroadcastEvent(
        event_id=uuid.uuid4(),
        ts=datetime.now(UTC),
        tenant_id=operator.tenant_id,
        principal_sub=operator.sub,
        principal_name=operator.name,
        target_name=target_name,
        op_id=_REFRESH_OP_ID,
        op_class=_REFRESH_OP_CLASS,
        result_status="ok",
        audit_id=audit_id,
        payload={
            "op_class": _REFRESH_OP_CLASS,
            "result_status": "ok",
            "target_id": str(result.target_id),
            "added_nodes": result.added_nodes,
            "added_edges": result.added_edges,
            "updated_nodes": result.updated_nodes,
            "updated_edges": result.updated_edges,
            "removed_nodes": result.removed_nodes,
            "removed_edges": result.removed_edges,
        },
        actor_sub=lineage.actor_sub,
        agent_session_id=lineage.agent_session_id,
        work_ref=lineage.work_ref,
    )
    await publish_event(event)


async def _apply_reconcile(
    *,
    operator: Operator,
    target_id: uuid.UUID,
    discovered_by: str,
    hints: TopologyHints,
    audit_id: uuid.UUID,
    started: float,
    no_populator_for_product: str | None,
    populated_products: tuple[str, ...] | None,
) -> RefreshResult:
    """Run the node + edge diff + history writes + audit write in one txn.

    A failure anywhere inside the ``session.begin()`` block raises out
    of it, rolling everything (inserts, updates, soft-deletes, the
    paired ``*_history`` rows, the audit row) back together — the
    graph is never left half-applied, no history row lands for a live
    mutation that did not commit, and no audit row lands for a refresh
    that didn't commit. The diff-on-write hook (G9.3-T2 #857) emits one
    ``graph_node_history`` row per added / updated / removed node and
    one ``graph_edge_history`` row per added / updated / removed edge,
    all carrying the pre-allocated ``audit_id`` so a downstream
    operator can join history back against ``audit_log`` to recover
    the causing principal / session / target.
    """
    tenant_id = operator.tenant_id
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        now = datetime.now(UTC)
        (
            added_nodes,
            updated_nodes,
            removed_nodes,
            live_key_to_id,
            all_key_to_id,
        ) = await _reconcile_nodes(
            session,
            tenant_id=tenant_id,
            target_id=target_id,
            discovered_by=discovered_by,
            nodes=hints.nodes,
            now=now,
            audit_id=audit_id,
        )
        added_edges, updated_edges, removed_edges = await _reconcile_edges(
            session,
            tenant_id=tenant_id,
            discovered_by=discovered_by,
            edges=hints.edges,
            live_node_key_to_id=live_key_to_id,
            all_node_key_to_id=all_key_to_id,
            now=now,
            audit_id=audit_id,
        )
        result = RefreshResult(
            target_id=target_id,
            added_nodes=added_nodes,
            added_edges=added_edges,
            updated_nodes=updated_nodes,
            updated_edges=updated_edges,
            removed_nodes=removed_nodes,
            removed_edges=removed_edges,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            no_populator_for_product=no_populator_for_product,
            populated_products=populated_products,
        )
        await _write_audit_and_broadcast(
            session=session,
            audit_id=audit_id,
            operator=operator,
            target_id=target_id,
            result=result,
        )
    return result


def _has_populator(connector_cls: type[Connector]) -> bool:
    """Return ``True`` when *connector_cls* overrides the base populator no-op.

    :meth:`Connector.discover_topology` ships a base-class default that
    returns an empty :class:`TopologyHints` (G9.1-T2 #449) so every
    connector stays refreshable without per-product topology work. That
    default made a coverage gap indistinguishable from a clean no-op on
    the wire (#2093); the identity check against the base function is
    the discriminator. Function identity (not ``hasattr``) is the right
    probe: an override anywhere in the MRO — including the
    operator-aware keyword-only variant on ``KubernetesConnector`` —
    replaces the class attribute, while auto-shim subclasses that
    inherit the base default compare identical.
    """
    return connector_cls.discover_topology is not Connector.discover_topology


def _populated_products() -> tuple[str, ...]:
    """Sorted product slugs whose registered connector ships a populator.

    Derived from the v2 registry snapshot (v1 registrations mirror into
    v2 as ``(product, "", "")``), deduped across version/impl entries.
    Registry state is process-local and small (tens of entries), so the
    scan per no-populator refresh is negligible.
    """
    return tuple(
        sorted(
            {
                product
                for (product, _version, _impl_id), cls in all_connectors_v2().items()
                if product and _has_populator(cls)
            }
        )
    )


async def _invoke_discover_topology(
    connector: Connector,
    target: Any,
    operator: Operator,
) -> TopologyHints:
    """Call ``connector.discover_topology(target)``, forwarding the operator when accepted.

    G0.14-T12 (#1201) lands the first
    :class:`~meho_backplane.connectors.base.Connector` override that needs
    the acting operator in scope — the populator on
    :class:`~meho_backplane.connectors.kubernetes.connector.KubernetesConnector`
    re-uses the per-target ``_get_api_client(target, operator)`` chain to
    read the cluster's namespaces + nodes under the per-tenant system
    operator the scheduler already synthesises. Out of scope for that
    Task: changing the :class:`Connector` ABC signature (refresh-service
    internal, not a connector-level contract change).

    The refresh service therefore forwards ``operator`` as a
    **refresh-private bound keyword argument** when the override
    declares it. Connectors that inherit the base no-op default (every
    shipped connector except K8s as of v0.7.x) keep their
    ``(self, target)`` signature and are invoked unchanged. Signature
    introspection on the bound method is the same shape the K8s
    dispatcher shim already uses in
    :meth:`~meho_backplane.connectors.kubernetes.connector.KubernetesConnector.execute`
    to detect operator-aware typed handlers.
    """
    method = connector.discover_topology
    params = inspect.signature(method).parameters
    if "operator" in params:
        # Forwarded as a refresh-private bound keyword argument; the
        # Connector ABC declares ``discover_topology(self, target)`` so
        # mypy sees the base signature here. Overrides that opt into
        # the keyword (currently KubernetesConnector — G0.14-T12 #1201)
        # declare ``operator`` keyword-only so this call lands on the
        # override unambiguously.
        return await method(target, operator=operator)  # type: ignore[call-arg]
    return await method(target)


async def refresh_target_topology(
    target: Any,
    operator: Operator,
) -> RefreshResult:
    """Discover ``target``'s topology and reconcile it into the graph.

    Resolves the connector for *target*, calls its
    :meth:`~meho_backplane.connectors.base.Connector.discover_topology`,
    and applies the diff against the existing ``graph_node`` +
    ``graph_edge`` rows for ``(operator.tenant_id, target.id)`` in one
    transaction. A failure anywhere in resolve / discover / reconcile /
    audit rolls the whole transaction back — the graph is never left
    half-applied and no audit row lands for a refresh that didn't
    commit.

    The audit row commits inside the reconcile transaction (synchronous
    audit). The broadcast event publishes after commit and is fail-open.

    Args:
        target: The :class:`~meho_backplane.db.models.Target` ORM row (or
            a duck-typed object exposing ``.id`` / ``.name`` /
            ``.product`` / ``.fingerprint``) to refresh.
        operator: The acting identity. Supplies the tenant scope
            (``operator.tenant_id``) every reconciled row is written
            under and the ``audit_log`` attribution. The scheduler
            synthesises a per-tenant system operator; the on-demand CLI
            path forwards the authenticated operator.

    Returns:
        A :class:`RefreshResult` with the disjoint per-class counts.
        When the resolved connector ships no topology populator, the
        result additionally carries ``no_populator_for_product`` +
        ``populated_products`` so the all-zero counts are readable as a
        coverage gap rather than a clean no-op (#2093).
    """
    started = time.perf_counter()
    target_id: uuid.UUID = target.id
    target_name: str = getattr(target, "name", str(target_id))
    discovered_by = getattr(target, "product", "unknown")
    tenant_id = operator.tenant_id

    connector_cls = resolve_connector(target)
    connector: Connector = get_or_create_connector_instance(connector_cls)
    hints: TopologyHints = await _invoke_discover_topology(connector, target, operator)

    # #2093 — discriminate the two all-zero-count no-op classes. A
    # connector inheriting the base discover_topology no-op returns an
    # empty snapshot every time; stamping the gap on the result (rather
    # than skipping the reconcile) keeps the refresh semantics intact —
    # the empty snapshot still soft-deletes stale nodes this target
    # adopted earlier, and the audit/broadcast contract is unchanged.
    no_populator_for_product: str | None = None
    populated_products: tuple[str, ...] | None = None
    if not _has_populator(connector_cls):
        no_populator_for_product = discovered_by
        populated_products = _populated_products()

    audit_id = uuid.uuid4()
    result = await _apply_reconcile(
        operator=operator,
        target_id=target_id,
        discovered_by=discovered_by,
        hints=hints,
        audit_id=audit_id,
        started=started,
        no_populator_for_product=no_populator_for_product,
        populated_products=populated_products,
    )

    # Transaction committed (audit row included). Broadcast is fail-open.
    try:
        await _publish_refresh_event(
            audit_id=audit_id,
            operator=operator,
            target_name=target_name,
            result=result,
        )
    except Exception:
        _log.exception(
            "topology_refresh_broadcast_failed",
            target_id=str(target_id),
            tenant_id=str(tenant_id),
        )

    _log.info(
        "topology_refresh_applied",
        target_id=str(target_id),
        tenant_id=str(tenant_id),
        added_nodes=result.added_nodes,
        added_edges=result.added_edges,
        updated_nodes=result.updated_nodes,
        updated_edges=result.updated_edges,
        removed_nodes=result.removed_nodes,
        removed_edges=result.removed_edges,
        duration_ms=round(result.duration_ms, 2),
        no_populator_for_product=result.no_populator_for_product,
    )
    return result
