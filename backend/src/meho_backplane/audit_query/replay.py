# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-session audit replay (G8.2-T3).

:func:`replay_session` reconstructs one agent session as a chronologically
ordered parent/child tree of :class:`~meho_backplane.audit_query.schemas.ReplayNode`
nodes. It is the read brain behind ``meho audit replay <session-id>`` (T5 CLI),
the REST replay route (T4), and the MCP replay surface (T6).

Two-step shape
==============

1. **Fetch the closure (SQL).** A SQLAlchemy 2.0 recursive CTE seeds on the
   session anchor — ``agent_session_id = :session_id AND tenant_id =
   :tenant_id`` — and walks ``child.parent_audit_id = anchor.id`` so any row
   linked into the session graph by lineage is included even when its own
   ``agent_session_id`` is NULL (e.g. a composite ``dispatch_child`` row whose
   contextvar didn't propagate). The tenant scope is re-asserted on the
   recursive arm so a cross-tenant ``parent_audit_id`` can never pull in a
   foreign-tenant row. This is the first recursive CTE in the codebase; it
   follows the SQLAlchemy 2.0 Core API (``select(...).cte(recursive=True)`` +
   ``CTE.union_all(...)``).

2. **Assemble the tree (Python).** Rows are bucketed by ``parent_audit_id``
   (NULL, or a parent id outside the fetched set, → root), each bucket sorted
   by ``(occurred_at, id)``, then walked depth-first assigning ``depth``. The
   walk is capped at ``max_depth`` so a self-referential row
   (``parent_audit_id == id``) or a multi-row cycle terminates instead of
   recursing forever — a node at the cap keeps its own row but its ``children``
   is truncated.

The closure (anchor plus descendants) is deduplicated by ``id`` before tree
assembly: a recursive CTE re-emits an anchor row that is also a descendant of
another anchor row, and a node that participates in a cycle is reached more
than once.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from meho_backplane.db.models import AuditLog, Target

from .query import _build_audit_entry
from .schemas import ReplayNode

__all__ = [
    "replay_session",
]


async def replay_session(
    session_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    session: AsyncSession,
    max_depth: int = 20,
) -> list[ReplayNode]:
    """Reconstruct one agent session as a chronological parent/child tree.

    :param session_id: the ``agent_session_id`` to anchor on.
    :param tenant_id: mandatory keyword-only tenant boundary — sourced from the
        validated JWT, never from client input. Asserted on both the anchor and
        the recursive arm so cross-tenant lineage cannot leak.
    :param session: the active :class:`AsyncSession`.
    :param max_depth: defensive cap on tree depth (cycle / runaway defence). A
        node reached at this depth keeps its own row but its ``children`` is
        left empty.
    :returns: the session's root nodes, ascending by ``(occurred_at, id)``.
        Empty list when the session id matches no tenant-scoped rows.

    Never raises ``UnsupportedFilterError`` — replay is a positive query, not a
    filtered list.
    """
    rows = await _fetch_session_closure(
        session_id,
        tenant_id=tenant_id,
        session=session,
        max_depth=max_depth,
    )
    return _assemble_tree(rows, max_depth=max_depth)


async def _fetch_session_closure(
    session_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    session: AsyncSession,
    max_depth: int,
) -> list[tuple[AuditLog, str | None]]:
    """Fetch every audit row reachable for the session, tenant-scoped.

    Builds the closure with a recursive CTE: the non-recursive arm selects the
    anchor ids (``agent_session_id = :session_id``) at depth 0, and the
    recursive arm joins child rows on ``child.parent_audit_id = closure.id`` at
    ``closure.depth + 1``. Both arms keep the ``tenant_id`` predicate so a
    forged cross-tenant ``parent_audit_id`` cannot widen the closure beyond the
    caller's tenant.

    The recursive arm is **depth-bounded** (``closure.depth < max_depth``).
    This bound is load-bearing, not cosmetic: a cyclic ``parent_audit_id``
    graph (``A.parent = B`` / ``B.parent = A``, or a self-loop) makes an
    unbounded ``UNION ALL`` recursive CTE loop forever — both SQLite and
    PostgreSQL execute the recursion at the C level with no built-in cycle
    detection, so the Python-side cap in :func:`_assemble_tree` would never get
    a chance to run. Bounding the CTE itself is what guarantees termination;
    the Python cap is the second line of defence on the assembled tree.

    Only the row ids flow through the CTE; the full :class:`AuditLog` rows (plus
    the denormalized ``target_name``) are fetched in a single follow-up select
    keyed on the deduplicated id set.
    """
    closure_anchor = (
        sa.select(AuditLog.id, sa.literal(0).label("depth"))
        .where(
            AuditLog.agent_session_id == session_id,
            AuditLog.tenant_id == tenant_id,
        )
        .cte(recursive=True, name="session_closure")
    )

    child = aliased(AuditLog, name="child")
    closure = closure_anchor.union_all(
        sa.select(child.id, (closure_anchor.c.depth + 1).label("depth")).where(
            child.parent_audit_id == closure_anchor.c.id,
            child.tenant_id == tenant_id,
            closure_anchor.c.depth < max_depth,
        ),
    )

    # The CTE re-emits ids reached by more than one path (an anchor row that is
    # also a descendant, a node in a cycle). DISTINCT collapses them so the
    # follow-up fetch and Python-side bucketing each see every row once.
    closure_ids = sa.select(closure.c.id).distinct().scalar_subquery()

    stmt = (
        sa.select(AuditLog, Target.name.label("target_name"))
        .outerjoin(
            Target,
            sa.and_(
                AuditLog.target_id == Target.id,
                Target.tenant_id == tenant_id,
            ),
        )
        .where(AuditLog.id.in_(closure_ids))
    )

    result = await session.execute(stmt)
    return [(row.AuditLog, row.target_name) for row in result.all()]


def _assemble_tree(
    rows: list[tuple[AuditLog, str | None]],
    *,
    max_depth: int,
) -> list[ReplayNode]:
    """Build the chronological forest from the flat fetched closure.

    A row is a *root* when its ``parent_audit_id`` is NULL, points outside the
    fetched set (the parent lives in a different tenant / session, or was
    pruned beyond the closure's depth bound), or equals its own id (self-loop).
    Children of each parent — and the root list — are ordered by
    ``(occurred_at, id)``.

    Cycle defence and depth-capping are separate concerns. A ``seen`` path-set
    drops back-edges so a node already on the current root-to-node path is never
    re-descended — that alone makes any cycle terminate. The ``max_depth`` cap
    is the rendering bound: a node *at* the cap keeps its row but its
    ``children`` is empty, and rows deeper than the cap are not recorded.

    A pure cycle with no external entry (every member's parent is inside the
    set) yields no parent-rule root; those members are promoted to roots in
    chronological order after the natural roots are walked, so every fetched row
    surfaces (see :meth:`_TreeBuilder.build_forest`).
    """
    return _TreeBuilder(rows, max_depth=max_depth).build_forest()


class _TreeBuilder:
    """Single-use builder that turns a flat row closure into a replay forest.

    Holds the per-call traversal state (the entry map, the parent→children
    buckets, and the ``reached`` set) so the recursive helpers stay small and
    the orchestration in :meth:`build_forest` reads top-to-bottom.
    """

    def __init__(
        self,
        rows: list[tuple[AuditLog, str | None]],
        *,
        max_depth: int,
    ) -> None:
        self._max_depth = max_depth
        self._entries = {row.id: _build_audit_entry(row, name) for row, name in rows}
        self._children: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
        self._root_ids: list[uuid.UUID] = []
        for row, _ in rows:
            parent_id = row.parent_audit_id
            if parent_id is not None and parent_id in self._entries and parent_id != row.id:
                self._children[parent_id].append(row.id)
            else:
                self._root_ids.append(row.id)
        self._reached: set[uuid.UUID] = set()

    def _sort_key(self, node_id: uuid.UUID) -> tuple[datetime, uuid.UUID]:
        entry = self._entries[node_id]
        return (entry.ts, entry.id)

    def _mark_reached(self, node_id: uuid.UUID, seen: frozenset[uuid.UUID]) -> None:
        """Mark every node in the cycle-broken subtree as reached.

        Independent of ``max_depth`` — a descendant beyond the rendering cap is
        still *reached* (it has a real parent path to a root) and must not be
        misclassified as an unreachable cycle orphan. The ``seen`` path-set
        bounds this walk on cycles exactly as :meth:`_render` does.
        """
        self._reached.add(node_id)
        for child_id in self._children.get(node_id, ()):
            if child_id not in seen:
                self._mark_reached(child_id, seen | {child_id})

    def _render(self, node_id: uuid.UUID, depth: int, seen: frozenset[uuid.UUID]) -> ReplayNode:
        children: list[ReplayNode] = []
        if depth < self._max_depth:
            for child_id in sorted(self._children.get(node_id, ()), key=self._sort_key):
                if child_id not in seen:
                    children.append(self._render(child_id, depth + 1, seen | {child_id}))
        return ReplayNode(**self._entries[node_id].model_dump(), depth=depth, children=children)

    def _emit_root(self, node_id: uuid.UUID) -> ReplayNode:
        self._mark_reached(node_id, frozenset({node_id}))
        return self._render(node_id, 0, frozenset({node_id}))

    def build_forest(self) -> list[ReplayNode]:
        forest = [self._emit_root(rid) for rid in sorted(self._root_ids, key=self._sort_key)]
        # Orphaned cycle members — fetched rows with no acyclic path to any
        # natural root because their whole component is a cycle. Promote each
        # (chronologically) so the row is not silently dropped; skip any the
        # preceding promotions already reached.
        orphans = sorted(
            (nid for nid in self._entries if nid not in self._reached),
            key=self._sort_key,
        )
        forest.extend(self._emit_root(nid) for nid in orphans if nid not in self._reached)
        return forest
