# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Guarded hard-delete of a manually-seeded ``graph_node`` (#2485).

Initiative #2494 (G0.32). :func:`~meho_backplane.topology.nodes.create_or_get_node`
seeds ``graph_node`` rows but no delete counterpart existed on any
surface, so a mis-seeded or probe-residue manual node persisted
indefinitely: refresh reconciliation only touches nodes adopted onto the
refreshed target, and soft-deleted nodes stay reachable in traversals
anyway (``topology/query.py``). :func:`delete_node` is the guarded
hard-delete the MCP tool ``meho.topology.delete_node`` and the REST route
``DELETE /api/v1/topology/nodes/{node_id}`` both front.

The verb is the delete-half mirror of ``create_or_get_node`` and reuses
its disciplines: session-first (the caller passes an :class:`AsyncSession`
with no active transaction and the function opens its own
``session.begin()``), audit-id pre-allocation so the ``removed`` history
tombstone and the ``audit_log`` row share one id, and a fail-open
broadcast after commit.

Three guards, in order (mirroring the §3 auto-edge rule the edge-half
:func:`~meho_backplane.topology.annotate.unannotate_edge` enforces):

1. **404** — no ``graph_node`` matches ``node_id`` under the operator's
   tenant (:class:`NodeNotFoundForDeleteError`). Cross-tenant ids are
   indistinguishable from missing ones.
2. **409 probe_owned** — the row is not an operator-owned manual seed
   (:class:`NodeNotDeletableError`): ``source != 'curated'`` (probe-
   derived, incl. auto-discovered inner-graph nodes refresh owns) **or**
   ``target_id IS NOT NULL`` (adopted onto a registered target; would
   resurrect on the next probe). Only ``source='curated'`` **and**
   ``target_id IS NULL`` rows are hard-deletable.
3. **409 has-edges** — at least one live ``graph_edge``
   (``last_seen IS NOT NULL``) references the node
   (:class:`NodeHasLiveEdgesError`), echoing the blocking edge ids. The
   DB ``ON DELETE CASCADE`` on ``graph_edge`` stays a backstop only
   (``db/models.py``): a bare cascade would drop the referencing edges
   without their ``graph_edge_history`` ``removed`` tombstones. The
   caller unannotates the edges first.

The happy path, in one transaction: one ``graph_node_history``
``removed`` tombstone (``before=<final row>`` / ``after=None``), one
``audit_log`` row (``op_id='topology.delete_node'``, ``op_class='write'``,
``method='DELETE_NODE'``), then the hard delete of the live row. The
node's ``graph_node_history`` rows (the fresh tombstone included) survive
the delete with ``node_id`` NULL via the ``ON DELETE SET NULL`` FK
(``db/models.py``) so the timeline facet still renders them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import or_, select

from meho_backplane.auth.delegation import resolve_actor_sub
from meho_backplane.broadcast import BroadcastEvent, publish_event
from meho_backplane.db.models import (
    AuditLog,
    GraphEdge,
    GraphHistoryChangeKind,
    GraphNode,
)
from meho_backplane.operations._audit import resolve_broadcast_lineage
from meho_backplane.topology.history import node_snapshot, record_node_change

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from meho_backplane.auth.operator import Operator

__all__ = [
    "DeleteNodeResult",
    "NodeHasLiveEdgesError",
    "NodeNotDeletableError",
    "NodeNotFoundForDeleteError",
    "delete_node",
]

_log = structlog.get_logger(__name__)

#: Canonical op-id — mirrored into ``audit_log.path`` + the broadcast
#: event's ``op_id`` (the ``meho status --watch`` viewer key), and the
#: exact string the dispatcher's own ``method='DISPATCH'`` row correlates
#: on. Matches :data:`nodes._CREATE_NODE_OP_ID`'s convention.
_DELETE_NODE_OP_ID = "topology.delete_node"

#: ``op_class`` for the verb — set explicitly (not derived via
#: :func:`broadcast.events.classify_op`) because ``.delete_node`` is not
#: a classifier write suffix and would fall through to ``other``. Same
#: rationale as :data:`nodes._OP_CLASS`.
_OP_CLASS = "write"

#: Non-HTTP audit method token (chassis convention: a non-HTTP write
#: records ``method`` as a verb token and ``path`` as the op_id).
_AUDIT_METHOD = "DELETE_NODE"


class NodeNotFoundForDeleteError(ValueError):
    """No ``graph_node`` matched ``node_id`` in the caller's tenant.

    Raised by :func:`delete_node` when the id resolves to zero rows under
    ``operator.tenant_id``. A node that exists only in another tenant
    resolves here, never to that tenant's row — cross-tenant ids are
    indistinguishable from missing ones to the caller, the same
    tenant-boundary contract
    :func:`~meho_backplane.topology.annotate.unannotate_edge` holds for
    its id selector. The REST layer maps it to a 404 ``node_not_found``;
    the MCP dispatcher maps it to JSON-RPC ``-32602``.
    """

    def __init__(self, node_id: uuid.UUID) -> None:
        self.node_id = node_id
        super().__init__(f"no graph_node matched id {node_id} in this tenant")


class NodeNotDeletableError(ValueError):
    """The targeted node is probe-owned and refuses a hard delete.

    Raised by :func:`delete_node` when the row is not an operator-owned
    manual seed. Two schema-grounded signals mark probe ownership, and
    either one refuses the delete:

    * ``source != 'curated'`` — the row is probe-derived (T3 refresh
      writes ``source='auto'``); deleting it is meaningless because the
      next refresh re-observes and re-creates it. This includes
      auto-discovered inner-graph nodes (``target_id IS NULL`` but
      ``source='auto'``), which refresh reconciliation owns.
    * ``target_id IS NOT NULL`` — the row is adopted onto a registered
      target by the refresh service and resurrects on the next probe of
      that target. A curated node never carries a ``target_id`` (the
      create path sets it ``None`` and refresh never adopts a curated
      row), so this clause only ever fires for an ``auto`` row today —
      but pinning it explicitly keeps the guard correct if a future
      write path ever adopts a curated node onto a target.

    Only ``source='curated'`` **and** ``target_id IS NULL`` rows — the
    manual seeds :func:`~meho_backplane.topology.nodes.create_or_get_node`
    writes — are hard-deletable. Mirrors the §3 auto-edge rule
    :class:`~meho_backplane.topology.annotate.AutoEdgeDeletionError`
    enforces on the edge half. The REST layer maps it to a 409
    ``probe_owned_node``; the MCP dispatcher maps it to ``-32602``.
    """

    def __init__(self, node_id: uuid.UUID, *, source: str, target_id: uuid.UUID | None) -> None:
        self.node_id = node_id
        self.source = source
        self.target_id = target_id
        super().__init__(
            f"graph_node {node_id} is probe-owned (source={source!r}, "
            f"target_id={target_id}); refresh reconciliation owns it and it "
            "would resurrect on the next probe. Only manually-seeded curated "
            "nodes (source='curated', target_id IS NULL) are hard-deletable."
        )


class NodeHasLiveEdgesError(ValueError):
    """The targeted node still has live edges; the delete is refused.

    Raised by :func:`delete_node` when at least one ``graph_edge`` with
    ``last_seen IS NOT NULL`` references the node on either endpoint. The
    DB ``ON DELETE CASCADE`` on ``graph_edge.from_node_id`` /
    ``.to_node_id`` exists only as a backstop for tenant purges + test
    cleanup (``db/models.py``); letting it fire on an operator delete
    would drop the referencing edges **without** their
    ``graph_edge_history`` ``removed`` tombstones, silently losing the
    curated cross-system context the graph exists to hold. The service
    refuses instead and echoes the blocking ``edge_ids`` so the caller
    can :func:`~meho_backplane.topology.annotate.unannotate_edge` them
    first — the same unannotate-first discipline the edge half enforces.
    The REST layer maps it to a 409 ``node_has_edges``; the MCP
    dispatcher maps it to ``-32602``.
    """

    def __init__(self, node_id: uuid.UUID, *, edge_ids: list[uuid.UUID]) -> None:
        self.node_id = node_id
        self.edge_ids = edge_ids
        rendered = ", ".join(str(e) for e in edge_ids)
        super().__init__(
            f"graph_node {node_id} has {len(edge_ids)} live edge(s) "
            f"({rendered}); unannotate them before deleting the node."
        )


@dataclass(frozen=True, slots=True)
class DeleteNodeResult:
    """Outcome of one :func:`delete_node` call.

    Carries the deleted row's identity (the live row is gone post-commit,
    so the caller cannot re-read it). ``kind`` / ``name`` are the
    pre-delete snapshot the MCP / REST fronts echo so an operator sees
    *what* was removed without a second lookup — the same ``{kind, name}``
    pair the ``removed`` history tombstone's ``before`` snapshot
    preserves.
    """

    node_id: uuid.UUID
    kind: str
    name: str


def _build_payload(*, node_id: uuid.UUID, kind: str, name: str) -> dict[str, Any]:
    """Build the shared audit / broadcast payload for a delete.

    The same dict lands in ``audit_log.payload`` (full row) and in the
    broadcast event (``op_class='write'`` defaults to full detail).
    Carries the pre-delete ``{kind, name}`` so the audit trail records
    what was removed without joining back against the now-gone live row.
    """
    return {
        "op_id": _DELETE_NODE_OP_ID,
        "op_class": _OP_CLASS,
        "node_id": str(node_id),
        "kind": kind,
        "name": name,
    }


def _build_audit_row(
    *,
    audit_id: uuid.UUID,
    operator: Operator,
    target_id: uuid.UUID | None,
    payload: dict[str, Any],
) -> AuditLog:
    """Construct one ``audit_log`` row for a delete_node call.

    Mirrors :func:`nodes._build_audit_row` (status 200, ``method`` as
    verb token, ``path`` as op_id) and reuses the pre-allocated
    ``audit_id`` so the ``removed`` history row references the same
    ``audit_log`` row this call writes.
    """
    return AuditLog(
        id=audit_id,
        occurred_at=datetime.now(UTC),
        operator_sub=operator.sub,
        actor_sub=resolve_actor_sub(),
        tenant_id=operator.tenant_id,
        target_id=target_id,
        method=_AUDIT_METHOD,
        path=_DELETE_NODE_OP_ID,
        status_code=200,
        request_id=None,
        duration_ms=Decimal("0.00"),
        payload=payload,
    )


async def _publish(
    *,
    audit_id: uuid.UUID,
    operator: Operator,
    node_name: str,
    payload: dict[str, Any],
) -> None:
    """Fail-open broadcast publish (mirrors :func:`nodes._publish`).

    Emits the broadcast event and swallows publisher failure so a broken
    stream never rolls back a committed delete.
    """
    try:
        lineage = resolve_broadcast_lineage()
        event = BroadcastEvent(
            event_id=uuid.uuid4(),
            ts=datetime.now(UTC),
            tenant_id=operator.tenant_id,
            principal_sub=operator.sub,
            principal_name=operator.name,
            target_name=node_name,
            op_id=_DELETE_NODE_OP_ID,
            op_class=_OP_CLASS,
            result_status="ok",
            audit_id=audit_id,
            payload=payload,
            actor_sub=lineage.actor_sub,
            agent_session_id=lineage.agent_session_id,
            work_ref=lineage.work_ref,
        )
        await publish_event(event)
    except Exception:
        _log.exception(
            "topology_delete_node_broadcast_failed",
            op_id=_DELETE_NODE_OP_ID,
            tenant_id=str(operator.tenant_id),
        )


async def _live_edge_ids(
    session: AsyncSession, *, tenant_id: uuid.UUID, node_id: uuid.UUID
) -> list[uuid.UUID]:
    """Return the ids of live edges referencing the node on either endpoint.

    ``last_seen IS NOT NULL`` is the live-row filter the edge inventory
    (:func:`~meho_backplane.topology.query.list_edges`) and the
    include_stale traversals share; a soft-deleted edge does not block
    the delete because it is already out of the live graph. Ordered by
    id so the refusal's ``edge_ids`` are deterministic for the caller
    and for tests.
    """
    stmt = (
        select(GraphEdge.id)
        .where(
            GraphEdge.tenant_id == tenant_id,
            GraphEdge.last_seen.is_not(None),
            or_(
                GraphEdge.from_node_id == node_id,
                GraphEdge.to_node_id == node_id,
            ),
        )
        .order_by(GraphEdge.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _delete_in_txn(
    session: AsyncSession,
    operator: Operator,
    *,
    node_id: uuid.UUID,
    audit_id: uuid.UUID,
) -> tuple[DeleteNodeResult, dict[str, Any]]:
    """In-transaction body of :func:`delete_node`.

    Resolves + guards the row, emits the ``removed`` tombstone and the
    audit row, then hard-deletes the live row. Returns
    ``(result, audit_payload)`` so the caller can publish the broadcast
    event after commit. Raises the three guard errors documented on
    :func:`delete_node`.
    """
    node = (
        await session.execute(
            select(GraphNode).where(
                GraphNode.id == node_id,
                GraphNode.tenant_id == operator.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if node is None:
        raise NodeNotFoundForDeleteError(node_id)

    if node.source != "curated" or node.target_id is not None:
        raise NodeNotDeletableError(node_id, source=node.source, target_id=node.target_id)

    live_edges = await _live_edge_ids(session, tenant_id=operator.tenant_id, node_id=node_id)
    if live_edges:
        raise NodeHasLiveEdgesError(node_id, edge_ids=live_edges)

    # Capture the pre-delete identity + snapshot before the row is gone.
    # ``kind`` / ``name`` are read off the live row now because the
    # returned result and the fail-open broadcast both run after the
    # delete.
    kind = node.kind
    name = node.name
    target_id = node.target_id
    now = datetime.now(UTC)
    before = node_snapshot(node)

    # Diff-on-write hook (G9.3-T2 #857): one ``removed`` tombstone
    # (before=snapshot / after=None) so ``query_topology kind=timeline``
    # renders the delete the same way it renders a refresh soft-remove.
    # The tombstone (and every pre-existing history row for this node)
    # survives the hard-delete with ``node_id`` NULL via the ``ON DELETE
    # SET NULL`` FK; the ``before.id`` inside the snapshot preserves row
    # identity.
    record_node_change(
        session,
        node_id=node_id,
        tenant_id=operator.tenant_id,
        change_kind=GraphHistoryChangeKind.REMOVED,
        before=before,
        after=None,
        audit_id=audit_id,
        valid_from=now,
    )

    payload = _build_payload(node_id=node_id, kind=kind, name=name)
    session.add(
        _build_audit_row(
            audit_id=audit_id,
            operator=operator,
            target_id=target_id,
            payload=payload,
        )
    )

    await session.delete(node)
    await session.flush()

    return DeleteNodeResult(node_id=node_id, kind=kind, name=name), payload


async def delete_node(
    session: AsyncSession,
    operator: Operator,
    *,
    node_id: uuid.UUID,
) -> DeleteNodeResult:
    """Guarded hard-delete of a manually-seeded ``graph_node`` row.

    Resolves ``node_id`` tenant-scoped, refuses probe-owned rows and rows
    with live edges, then — in one transaction — writes a ``removed``
    history tombstone + one ``audit_log`` row and hard-deletes the live
    row. Publishes one :class:`BroadcastEvent` after commit (fail-open).

    Args:
        session: Caller-owned :class:`AsyncSession` with **no active
            transaction**. The function opens its own ``session.begin()``
            so the tombstone + audit + delete commit or roll back
            together (matches
            :func:`~meho_backplane.topology.nodes.create_or_get_node`).
        operator: The acting identity. Supplies the tenant scope and
            audit attribution. Role gating (``tenant_admin``) is the
            front layer's job (MCP / REST); the service trusts its
            caller.
        node_id: Primary-key selector of the row to remove.

    Returns:
        :class:`DeleteNodeResult` with the deleted node's id + the
        pre-delete ``kind`` / ``name``.

    Raises:
        NodeNotFoundForDeleteError: ``node_id`` does not resolve in the
            operator's tenant.
        NodeNotDeletableError: the row is probe-owned
            (``source != 'curated'`` or ``target_id IS NOT NULL``).
        NodeHasLiveEdgesError: at least one live ``graph_edge``
            references the node.
    """
    # Pre-allocate ``audit_id`` so the ``removed`` history row's
    # ``audit_id`` references the same ``audit_log`` row this call writes
    # (the chassis audit-id pre-allocation pattern shared with create /
    # refresh / annotate).
    audit_id = uuid.uuid4()

    async with session.begin():
        result, payload = await _delete_in_txn(
            session, operator, node_id=node_id, audit_id=audit_id
        )

    await _publish(audit_id=audit_id, operator=operator, node_name=result.name, payload=payload)
    return result
