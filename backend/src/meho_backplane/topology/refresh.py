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

import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.broadcast import BroadcastEvent, publish_event
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.resolver import resolve_connector
from meho_backplane.connectors.schemas import EdgeHint, NodeHint, TopologyHints
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, GraphEdge, GraphNode
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
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


def _node_key(kind: str, name: str) -> tuple[str, str]:
    """Natural key for a node within a ``(tenant_id, target scope)``."""
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


def _merge_edge_properties(
    current: Any,
    incoming: Any,
) -> dict[str, Any]:
    """Return ``incoming`` merged with the reserved markers from ``current``.

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
    sourced from ``incoming``. Only :func:`unannotate_edge` of the
    curated row clears the supersede mark (Initiative #364 §6).
    """
    merged = dict(incoming)
    current_dict = dict(current or {})
    for key in _RESERVED_MARKER_KEYS:
        if key in current_dict:
            merged[key] = current_dict[key]
    return merged


async def _reconcile_nodes(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    target_id: uuid.UUID,
    discovered_by: str,
    nodes: tuple[NodeHint, ...],
    now: datetime,
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
    * ``all_key_to_id`` — every node in the ``(tenant, target)`` scope,
      including ones soft-deleted this refresh or on a prior one. The
      edge pass needs this to map an *existing* edge's ``from/to`` node
      ids back to keys so it can decide whether that edge is still
      discovered (and to soft-delete it when it is not).
    """
    existing_rows = (
        (
            await session.execute(
                select(GraphNode).where(
                    GraphNode.tenant_id == tenant_id,
                    GraphNode.target_id == target_id,
                )
            )
        )
        .scalars()
        .all()
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
            new_id = uuid.uuid4()
            session.add(
                GraphNode(
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
            )
            live_key_to_id[key] = new_id
            all_key_to_id[key] = new_id
            added += 1
            continue
        # Present in both — refresh last_seen, and properties when changed.
        if row.last_seen is None or _properties_differ(row.properties, hint.properties):
            updated += 1
        row.properties = dict(hint.properties)
        row.last_seen = now
        live_key_to_id[key] = row.id

    for key, row in existing_by_key.items():
        if key in discovered_by_key:
            continue
        if row.last_seen is None:
            # Already soft-deleted on a prior refresh; not a fresh removal.
            continue
        row.last_seen = None
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


async def _reconcile_edges(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    discovered_by: str,
    edges: tuple[EdgeHint, ...],
    live_node_key_to_id: dict[tuple[str, str], uuid.UUID],
    all_node_key_to_id: dict[tuple[str, str], uuid.UUID],
    now: datetime,
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
            session.add(
                GraphEdge(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    from_node_id=from_id,
                    to_node_id=to_id,
                    kind=hint.kind,
                    source="auto",
                    properties=dict(hint.properties),
                    discovered_by=discovered_by,
                    first_seen=now,
                    last_seen=now,
                )
            )
            added += 1
            continue
        # Merge — not overwrite — so the §6 conflict markers
        # (``superseded_by`` / ``conflicts_with``) an operator's
        # annotation may have stamped on this row survive the refresh.
        # The wholesale-overwrite this used to do silently cleared the
        # sticky-supersede invariant; see :func:`_merge_edge_properties`.
        merged_properties = _merge_edge_properties(existing_edge.properties, hint.properties)
        if existing_edge.last_seen is None or _properties_differ(
            existing_edge.properties, merged_properties
        ):
            updated += 1
        existing_edge.properties = merged_properties
        existing_edge.last_seen = now

    for key, row in existing_by_key.items():
        if key in discovered_by_key:
            continue
        if row.last_seen is None:
            continue
        row.last_seen = None
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
) -> RefreshResult:
    """Run the node + edge diff + audit write in one transaction.

    A failure anywhere inside the ``session.begin()`` block raises out
    of it, rolling everything (inserts, updates, soft-deletes, the audit
    row) back together — the graph is never left half-applied and no
    audit row lands for a refresh that didn't commit.
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
        )
        added_edges, updated_edges, removed_edges = await _reconcile_edges(
            session,
            tenant_id=tenant_id,
            discovered_by=discovered_by,
            edges=hints.edges,
            live_node_key_to_id=live_key_to_id,
            all_node_key_to_id=all_key_to_id,
            now=now,
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
        )
        await _write_audit_and_broadcast(
            session=session,
            audit_id=audit_id,
            operator=operator,
            target_id=target_id,
            result=result,
        )
    return result


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
    """
    started = time.perf_counter()
    target_id: uuid.UUID = target.id
    target_name: str = getattr(target, "name", str(target_id))
    discovered_by = getattr(target, "product", "unknown")
    tenant_id = operator.tenant_id

    connector_cls = resolve_connector(target)
    connector: Connector = get_or_create_connector_instance(connector_cls)
    hints: TopologyHints = await connector.discover_topology(target)

    audit_id = uuid.uuid4()
    result = await _apply_reconcile(
        operator=operator,
        target_id=target_id,
        discovered_by=discovered_by,
        hints=hints,
        audit_id=audit_id,
        started=started,
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
    )
    return result
