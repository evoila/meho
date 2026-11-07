# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Link edges from a refresh to nodes that other targets found.

A connector sends :class:`~meho_backplane.connectors.schemas.LinkHint`
entries in its :class:`~meho_backplane.connectors.schemas.TopologyHints`
when it knows a name or an address but not the map node behind it (a DNS
server knows ``web01.example.com`` points at ``192.0.2.10``). This module
turns those links into ``resolves-to`` edges inside the refresh
transaction:

* Each name of a link is looked up among the tenant's live nodes of the
  link's ``to_kinds`` (case does not matter, ``legacy:`` rows and the
  refreshing target's own rows are left out). Exactly one match gives an
  edge. No match, or more than one, gives nothing: the refresh never makes
  a node for a link.
* The edges are keyed by node ids, not by names, because the far node can
  share its ``(kind, name)`` with a node in another scope.
* The refreshing target owns the ``resolves-to`` edges that start at its
  nodes and end at a node it does not own. A later refresh that no longer
  sends the link (or no longer finds the far node) marks the edge as gone.
  An operator's curated edge is never marked as gone; a refresh that sees
  it again only records the time.

The plain edge pass in :mod:`meho_backplane.topology.refresh` never sees
these edges: it only knows the refreshing target's own nodes, so an edge
whose far end is another target's node is outside its key map.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime
from typing import cast

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.connectors.schemas import EdgeHint, LinkHint, NodeKind
from meho_backplane.db.models import GraphEdge, GraphNode
from meho_backplane.topology.refresh import (
    _insert_discovered_edge,
    _refresh_curated_edge,
    _soft_remove_edge,
    _update_existing_auto_edge,
)

__all__ = ["LINK_EDGE_KIND", "reconcile_links"]

#: The one edge kind a link makes.
LINK_EDGE_KIND = "resolves-to"

_LinkKey = tuple[uuid.UUID, uuid.UUID, str]
_Candidate = tuple[uuid.UUID, str, str]


async def _load_candidates(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    links: tuple[LinkHint, ...],
    own_ids: set[uuid.UUID],
) -> dict[str, list[_Candidate]]:
    """Index the tenant's live nodes of the links' kinds by lower-case name."""
    kinds = sorted({kind for link in links for kind in link.to_kinds})
    rows = (
        await session.execute(
            select(GraphNode.id, GraphNode.kind, GraphNode.name).where(
                GraphNode.tenant_id == tenant_id,
                GraphNode.kind.in_(kinds),
                GraphNode.last_seen.is_not(None),
                or_(
                    GraphNode.source_scope.is_(None),
                    GraphNode.source_scope.not_like("legacy:%"),
                ),
            )
        )
    ).all()
    index: dict[str, list[_Candidate]] = defaultdict(list)
    for node_id, kind, name in rows:
        if node_id not in own_ids:
            index[name.lower()].append((node_id, kind, name))
    return index


def _discovered_links(
    links: tuple[LinkHint, ...],
    candidates: dict[str, list[_Candidate]],
    live_node_key_to_id: dict[tuple[str, str], uuid.UUID],
) -> dict[_LinkKey, tuple[uuid.UUID, uuid.UUID, EdgeHint]]:
    """Resolve each link name to one node; the first link for an edge wins."""
    out: dict[_LinkKey, tuple[uuid.UUID, uuid.UUID, EdgeHint]] = {}
    for link in links:
        from_id = live_node_key_to_id.get((link.from_kind, link.from_name))
        if from_id is None:
            continue
        for match_name in link.match_names:
            matches = [c for c in candidates.get(match_name.lower(), ()) if c[1] in link.to_kinds]
            if len(matches) != 1:
                continue
            to_id, to_kind, to_name = matches[0]
            key = (from_id, to_id, link.kind)
            if key in out:
                continue
            hint = EdgeHint(
                from_kind=link.from_kind,
                from_name=link.from_name,
                to_kind=cast(NodeKind, to_kind),
                to_name=to_name,
                kind=link.kind,
                properties=link.properties,
            )
            out[key] = (from_id, to_id, hint)
    return out


async def _load_existing_link_edges(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    own_ids: set[uuid.UUID],
) -> dict[_LinkKey, GraphEdge]:
    """Load the ``resolves-to`` edges from this target's nodes to other nodes."""
    rows = (
        (
            await session.execute(
                select(GraphEdge).where(
                    GraphEdge.tenant_id == tenant_id,
                    GraphEdge.kind == LINK_EDGE_KIND,
                    GraphEdge.from_node_id.in_(own_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    return {
        (row.from_node_id, row.to_node_id, row.kind): row
        for row in rows
        if row.to_node_id not in own_ids
    }


async def reconcile_links(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    discovered_by: str,
    links: tuple[LinkHint, ...],
    live_node_key_to_id: dict[tuple[str, str], uuid.UUID],
    all_node_key_to_id: dict[tuple[str, str], uuid.UUID],
    now: datetime,
    audit_id: uuid.UUID,
) -> tuple[int, int, int]:
    """Add, update and mark as gone this target's link edges.

    Returns ``(added, updated, removed)`` edge counts, which the refresh
    adds to its own edge counts.
    """
    own_ids = set(all_node_key_to_id.values())
    if not own_ids:
        return 0, 0, 0
    discovered: dict[_LinkKey, tuple[uuid.UUID, uuid.UUID, EdgeHint]] = {}
    if links:
        candidates = await _load_candidates(
            session, tenant_id=tenant_id, links=links, own_ids=own_ids
        )
        discovered = _discovered_links(links, candidates, live_node_key_to_id)
    existing = await _load_existing_link_edges(session, tenant_id=tenant_id, own_ids=own_ids)

    added = updated = removed = 0
    for key, (from_id, to_id, hint) in discovered.items():
        row = existing.get(key)
        if row is None:
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
        elif row.source == "curated":
            if _refresh_curated_edge(
                session, existing_edge=row, tenant_id=tenant_id, now=now, audit_id=audit_id
            ):
                updated += 1
        elif _update_existing_auto_edge(
            session,
            existing_edge=row,
            hint=hint,
            tenant_id=tenant_id,
            now=now,
            audit_id=audit_id,
        ):
            updated += 1

    for key, row in existing.items():
        if key in discovered or row.last_seen is None or row.source == "curated":
            continue
        _soft_remove_edge(session, row=row, tenant_id=tenant_id, now=now, audit_id=audit_id)
        removed += 1
    return added, updated, removed
