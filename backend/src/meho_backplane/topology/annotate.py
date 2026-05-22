# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Operator-curated annotation flow for the topology graph.

Initiative #364 (G9.2), Task #595 (T3). Annotate / unannotate are the
*write* halves of the curated edge surface: an operator asserts an
edge that no probe can derive (``k8s-sa-foo`` ``authenticates-via``
``vault-role-bar``) and the next blast-radius traversal sees it.

The two service functions :func:`annotate_edge` and
:func:`unannotate_edge` are the load-bearing primitives the REST
routes (T5 #597), CLI verbs (T6 #599) and MCP tools (T7 #598) all hang
off — they own:

1. **Endpoint resolution** via :func:`resolve_node` (#594). A name that
   does not exist in the operator's tenant raises
   :class:`NodeNotFoundError`; a name that is ambiguous across kinds
   raises :class:`AmbiguousNodeError`. Cross-tenant resolution is
   impossible by construction (the resolver scopes on
   ``operator.tenant_id``).

2. **Kind validation** against :class:`GraphEdgeKind` (#593). The closed
   v0.2 ten-kind vocabulary is the load-bearing modeling decision; a
   typo'd / made-up kind never reaches the DB-layer CHECK.

3. **Idempotent upsert** keyed on
   ``graph_edge_tenant_endpoints_kind_idx`` (``(tenant_id,
   from_node_id, to_node_id, kind)``). A repeat annotate of the same
   triple refreshes ``last_seen`` + ``properties`` instead of erroring
   with a unique-constraint violation.

4. **§6 conflict detection** (the recoverable-mistake invariant):

   * *Same kind, different endpoint* — auto edge from the same
     ``from_node_id`` of the same ``kind`` to a *different*
     ``to_node_id`` is marked ``properties.superseded_by =
     <curated-id>``. The supersede mark is **sticky** across refresh
     (preserved by ``refresh._reconcile_edges``); only an
     :func:`unannotate_edge` of the curated row clears it. Superseded
     auto edges are excluded from every traversal verb's recursive
     CTE (the ``properties->>'superseded_by' IS NULL`` guard in
     :mod:`meho_backplane.topology.query`).
   * *Incompatible kinds, same endpoint pair* — auto / curated edges
     for the same ``(tenant_id, from_node_id, to_node_id)`` of a
     *different* ``kind`` keep both rows; bidirectional
     ``properties.conflicts_with = [<other-id>]`` is appended on each
     side. The downstream policy layer is the consumer that resolves
     the contradiction; the topology layer only surfaces it.

   Both marker shapes live in ``graph_edge.properties`` JSONB — no
   schema change beyond T1's CHECK widening.

5. **Audit + broadcast** integration. One ``audit_log`` row per
   annotate / unannotate (``op_id='topology.annotate'`` /
   ``'topology.unannotate'``, ``op_class='write'`` — explicit, because
   the ``.annotate`` / ``.unannotate`` suffixes are not in
   :data:`broadcast.events._WRITE_SUFFIXES` and would otherwise
   classify as ``other``) + exactly one broadcast event carrying
   ``from`` / ``kind`` / ``to`` / ``note``. ``target_id`` is populated
   when the *from* node is itself a managed target (``target_id IS
   NOT NULL``).

The function is **session-first** and does not open its own
sessionmaker (the resolver convention from #594). Both write functions
own the commit + post-commit broadcast publish, mirroring
:func:`refresh_target_topology`'s "audit committed inside the
transaction, broadcast fail-open after commit" discipline. Callers
must pass a session with no active transaction; the function opens its
own ``session.begin()`` block.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select

from meho_backplane.broadcast import BroadcastEvent, publish_event
from meho_backplane.db.models import (
    AuditLog,
    GraphEdge,
    GraphEdgeKind,
    GraphHistoryChangeKind,
    GraphNode,
)
from meho_backplane.topology.history import (
    edge_snapshot,
    record_edge_change,
)
from meho_backplane.topology.resolvers import resolve_node

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from meho_backplane.auth.operator import Operator

__all__ = [
    "AnnotateConflictError",
    "AnnotatePlan",
    "AutoEdgeDeletionError",
    "InvalidEdgeKindError",
    "NodeRef",
    "UnannotateSelectorError",
    "annotate_edge",
    "annotate_edge_in_txn",
    "unannotate_edge",
]

_log = structlog.get_logger(__name__)

#: Canonical op-ids. Mirrored into ``audit_log.payload['op_id']`` and
#: into the broadcast event's ``op_id`` field; consumed by the
#: ``meho status --watch`` viewer the v0.2 chassis ships.
_ANNOTATE_OP_ID = "topology.annotate"
_UNANNOTATE_OP_ID = "topology.unannotate"

#: ``op_class`` for both verbs. Set explicitly rather than derived via
#: :func:`broadcast.events.classify_op` because ``.annotate`` /
#: ``.unannotate`` are not in :data:`broadcast.events._WRITE_SUFFIXES`
#: and the classifier would fall through to ``other``. Initiative
#: #364 §10 also locks the *write* classification: annotations carry
#: semantic context operators want to see in real time, so they
#: broadcast in full per the G6.1 default classifier — which the
#: ``write`` op-class enables.
_OP_CLASS = "write"

#: Non-HTTP audit method tokens (chassis convention: a non-HTTP write
#: records ``method`` as a verb token and ``path`` as the canonical
#: op_id, mirroring :data:`refresh._AUDIT_METHOD`).
_AUDIT_METHOD_ANNOTATE = "ANNOTATE"
_AUDIT_METHOD_UNANNOTATE = "UNANNOTATE"

#: Reserved keys in ``graph_edge.properties`` used by §6 conflict
#: detection. The refresh service must merge — not overwrite — these
#: when re-applying an EdgeHint to an existing row, otherwise the
#: sticky-supersede invariant breaks on the next probe.
_SUPERSEDED_BY = "superseded_by"
_CONFLICTS_WITH = "conflicts_with"


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NodeRef:
    """Operator-supplied reference to a :class:`GraphNode` endpoint.

    A small immutable pair the REST / CLI / MCP fronts hand to
    :func:`annotate_edge` and :func:`unannotate_edge` after parsing
    operator input. The wrapper keeps the call signature explicit
    (``from_ref``, ``to_ref``) without inventing a tuple convention or
    forcing every caller through ``**kwargs``.

    ``name`` is the ``graph_node.name`` to resolve; ``kind`` is the
    optional ``graph_node.kind`` pin per :func:`resolve_node`'s
    contract. Names that are ambiguous across kinds in the tenant
    require ``kind`` to be set or the call raises
    :class:`AmbiguousNodeError`.
    """

    name: str
    kind: str | None = None


# ---------------------------------------------------------------------------
# Typed errors (HTTP-agnostic — API layer maps to status codes)
# ---------------------------------------------------------------------------


class InvalidEdgeKindError(ValueError):
    """The supplied ``kind`` is not in the v0.2 :class:`GraphEdgeKind` enum.

    Raised by :func:`annotate_edge` before any DB write — the closed
    enum is the policy-layer grammar's first guard rail. The API layer
    maps it to a 422 with the supplied value and the candidate list in
    ``detail``; CLI maps it to a usage error with the same candidate
    list.
    """

    def __init__(self, kind: str) -> None:
        self.kind = kind
        valid = sorted(k.value for k in GraphEdgeKind)
        super().__init__(
            f"edge kind {kind!r} is not in the v0.2 vocabulary; valid kinds: {valid!r}"
        )


class AutoEdgeDeletionError(ValueError):
    """Refusal to :func:`unannotate_edge` an edge with ``source='auto'``.

    Hard-deleting an auto row is meaningless: the next refresh that
    still sees the edge re-creates it. The verb refuses with this
    typed error rather than silently no-op'ing or recreating the row.
    The API layer maps it to a 409 with the auto edge's id in
    ``detail`` so the operator sees the diagnostic without re-issuing
    a separate list-edges call.
    """

    def __init__(self, edge_id: uuid.UUID) -> None:
        self.edge_id = edge_id
        super().__init__(
            f"graph_edge {edge_id} is auto-discovered; refusing to delete "
            "(auto edges resurrect on next refresh; unannotate is a no-op)"
        )


class UnannotateSelectorError(ValueError):
    """Caller passed neither or both of ``edge_id`` and the triple selector.

    :func:`unannotate_edge` is a keyword-only API; exactly one of
    ``edge_id`` *or* the full ``(from_ref, kind, to_ref)`` triple must
    be supplied. Both / neither indicates a programming error in the
    front. The API layer maps it to a 422; CLI / MCP raise on their
    own arg-parse layer before reaching the service.
    """


class AnnotateConflictError(ValueError):
    """Raised when :func:`annotate_edge`'s conflict-detection sees an
    inconsistent state it cannot recover from automatically.

    Reserved for future use; the v0.2 conflict rules (§6 of #364) are
    designed to *always* land — same-kind/different-endpoint marks the
    auto row superseded, incompatible kinds coexist with bidirectional
    markers — so this class is currently unused. Kept on the public
    surface so a future widening of the conflict rules has a typed
    error to raise without an API/contract change.
    """


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_kind(kind: str) -> str:
    """Validate ``kind`` against :class:`GraphEdgeKind`; return the value.

    The enum membership check is the first guard rail — failing here
    avoids a more obscure DB ``CHECK`` violation later. Returns the
    canonical string form so subsequent code uses the StrEnum value,
    not the raw input (defensive against future kind aliases).
    """
    try:
        return GraphEdgeKind(kind).value
    except ValueError as exc:
        raise InvalidEdgeKindError(kind) from exc


async def _find_existing_edge(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    from_id: uuid.UUID,
    to_id: uuid.UUID,
    kind: str,
) -> GraphEdge | None:
    """Lookup an edge by the ``(tenant, from, to, kind)`` unique tuple.

    Anchored on ``graph_edge_tenant_endpoints_kind_idx``. Returns the
    row or ``None`` — same semantics ``existing_by_key`` uses inside
    :func:`refresh._reconcile_edges` so the idempotent-upsert branch
    reads identically.
    """
    stmt = select(GraphEdge).where(
        GraphEdge.tenant_id == tenant_id,
        GraphEdge.from_node_id == from_id,
        GraphEdge.to_node_id == to_id,
        GraphEdge.kind == kind,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _mark_same_kind_different_endpoint_superseded(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    curated_edge_id: uuid.UUID,
    from_id: uuid.UUID,
    to_id: uuid.UUID,
    kind: str,
) -> list[tuple[GraphEdge, dict[str, Any]]]:
    """Stamp ``superseded_by`` on auto edges that the curated row replaces.

    Conflict §6 rule 1: a curated edge ``runs-on(vm-A → host-Y)``
    supersedes any auto edge ``runs-on(vm-A → host-X)`` — same
    ``from_node_id`` + same ``kind`` + ``source='auto'`` + a
    *different* ``to_node_id``. Mutates ``properties`` in place
    (``GraphEdge.properties`` is a JSONB column; SQLAlchemy detects
    the change when the attribute is reassigned, so we reassign
    rather than mutating the inner dict).

    Returns a list of ``(edge, pre_mutation_snapshot)`` pairs — the
    caller emits one ``GraphEdgeHistory`` row per marked auto edge
    (G9.3-T2 #857 diff-on-write hook), using the pre-mutation
    snapshot as the history row's ``snapshot.before``. The audit /
    broadcast payload's ``superseded`` array is built from
    ``edge.id`` of each returned pair so the visibility of the
    conflict is not buried in a side effect.
    """
    stmt = select(GraphEdge).where(
        GraphEdge.tenant_id == tenant_id,
        GraphEdge.from_node_id == from_id,
        GraphEdge.kind == kind,
        GraphEdge.source == "auto",
        GraphEdge.to_node_id != to_id,
    )
    rows = (await session.execute(stmt)).scalars().all()
    marked: list[tuple[GraphEdge, dict[str, Any]]] = []
    for row in rows:
        # Capture the pre-mutation snapshot **before** reassigning
        # ``properties`` so the history row's ``before`` reflects the
        # row as the operator last saw it without the supersede mark.
        before = edge_snapshot(row)
        props = dict(row.properties or {})
        props[_SUPERSEDED_BY] = str(curated_edge_id)
        row.properties = props
        marked.append((row, before))
    return marked


async def _mark_incompatible_kinds_conflict(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    curated_edge: GraphEdge,
    from_id: uuid.UUID,
    to_id: uuid.UUID,
    kind: str,
) -> list[tuple[GraphEdge, dict[str, Any]]]:
    """Stamp bidirectional ``conflicts_with`` on edges of other kinds.

    Conflict §6 rule 2: a curated edge ``depends-on(svc → db)``
    *coexists* with an auto edge ``routes-through(svc → db)`` —
    same endpoint pair, incompatible kinds. Both rows survive; each
    one's ``properties.conflicts_with`` carries the other's id. The
    marker is a list so a single edge with several conflicting kinds
    accumulates every id (dedupe preserved).

    Returns ``(edge, pre_mutation_snapshot)`` pairs for every
    conflicting edge — caller emits one ``GraphEdgeHistory`` row per
    pair (G9.3-T2 #857). The curated row's own marker write is
    captured separately by the calling :func:`annotate_edge_in_txn`
    so the curated row's history snapshot reflects the post-conflict
    state.
    """
    stmt = select(GraphEdge).where(
        GraphEdge.tenant_id == tenant_id,
        GraphEdge.from_node_id == from_id,
        GraphEdge.to_node_id == to_id,
        GraphEdge.kind != kind,
    )
    rows = (await session.execute(stmt)).scalars().all()
    conflicting: list[tuple[GraphEdge, dict[str, Any]]] = []
    for row in rows:
        # Capture the pre-mutation snapshot before appending the marker.
        before = edge_snapshot(row)
        _append_conflict_marker(row, curated_edge.id)
        conflicting.append((row, before))

    if conflicting:
        # Bidirectional: the curated row points back at every
        # conflicting edge. The curated row's own history row is
        # written by the caller after both conflict scans complete,
        # so we do not capture an intermediate snapshot here.
        for other_edge, _ in conflicting:
            _append_conflict_marker(curated_edge, other_edge.id)
    return conflicting


def _append_conflict_marker(edge: GraphEdge, other_id: uuid.UUID) -> None:
    """Append ``other_id`` to ``edge.properties['conflicts_with']`` (dedupe).

    Reassigns ``edge.properties`` to a fresh dict (rather than mutating
    in place) so SQLAlchemy's change-detection picks up the JSONB
    write — the JSON column type does not auto-detect in-place
    mutations of the inner dict.
    """
    props = dict(edge.properties or {})
    raw = props.get(_CONFLICTS_WITH)
    current: list[str] = list(raw) if isinstance(raw, list) else []
    other_str = str(other_id)
    if other_str not in current:
        current.append(other_str)
    props[_CONFLICTS_WITH] = current
    edge.properties = props


def _clear_reciprocal_markers(
    edges: list[GraphEdge],
    *,
    removed_edge_id: uuid.UUID,
) -> None:
    """Drop references to ``removed_edge_id`` from the other edges' markers.

    On :func:`unannotate_edge` of a curated row we walk every edge
    that previously paired with it (either ``superseded_by`` ==
    removed-id, or ``removed-id`` appearing in ``conflicts_with``)
    and clear the back-reference so the auto row reappears in
    traversal and dangling ids do not linger.
    """
    removed_str = str(removed_edge_id)
    for edge in edges:
        props = dict(edge.properties or {})
        changed = False
        if props.get(_SUPERSEDED_BY) == removed_str:
            props.pop(_SUPERSEDED_BY)
            changed = True
        conflicts = props.get(_CONFLICTS_WITH)
        if isinstance(conflicts, list) and removed_str in conflicts:
            new_conflicts = [c for c in conflicts if c != removed_str]
            if new_conflicts:
                props[_CONFLICTS_WITH] = new_conflicts
            else:
                props.pop(_CONFLICTS_WITH)
            changed = True
        if changed:
            edge.properties = props


async def _find_edges_referencing(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    edge_id: uuid.UUID,
) -> list[GraphEdge]:
    """Return every edge with a ``superseded_by`` / ``conflicts_with`` ref
    to ``edge_id``.

    The properties JSONB scan is portable across JSON + JSONB (no
    PG-specific ``->>`` operator) so the unit suite running on
    aiosqlite + the integration suite on PG share one code path.
    """
    rows = (
        (await session.execute(select(GraphEdge).where(GraphEdge.tenant_id == tenant_id)))
        .scalars()
        .all()
    )
    removed_str = str(edge_id)
    out: list[GraphEdge] = []
    for row in rows:
        props = row.properties or {}
        if props.get(_SUPERSEDED_BY) == removed_str:
            out.append(row)
            continue
        conflicts = props.get(_CONFLICTS_WITH)
        if isinstance(conflicts, list) and removed_str in conflicts:
            out.append(row)
    return out


def _audit_payload(
    *,
    op_id: str,
    from_node: GraphNode,
    to_node: GraphNode,
    kind: str,
    edge_id: uuid.UUID,
    note: str | None,
    evidence_url: str | None,
    superseded: list[uuid.UUID],
    conflicts: list[uuid.UUID],
) -> dict[str, Any]:
    """Build the shared audit / broadcast payload.

    The same dict lands in ``audit_log.payload`` (full row) and in
    the broadcast event (``op_class='write'`` defaults to full
    detail per §10 of #364). ``superseded`` / ``conflicts`` give
    downstream visibility on what the assertion just rewrote — the
    diagnostic the recovery flow needs.
    """
    return {
        "op_id": op_id,
        "op_class": _OP_CLASS,
        "edge_id": str(edge_id),
        "from": {
            "id": str(from_node.id),
            "kind": from_node.kind,
            "name": from_node.name,
        },
        "to": {
            "id": str(to_node.id),
            "kind": to_node.kind,
            "name": to_node.name,
        },
        "kind": kind,
        "note": note,
        "evidence_url": evidence_url,
        "superseded": [str(i) for i in superseded],
        "conflicts": [str(i) for i in conflicts],
    }


def _build_audit_row(
    *,
    audit_id: uuid.UUID,
    operator: Operator,
    method: str,
    op_id: str,
    target_id: uuid.UUID | None,
    payload: dict[str, Any],
) -> AuditLog:
    """Construct one ``AuditLog`` row for an annotate / unannotate.

    Mirrors the columns ``refresh._write_audit_and_broadcast`` writes
    (status 200, ``method`` as verb token, ``path`` as op_id) and
    pre-allocates the ``audit_id`` so the broadcast event's
    ``audit_id`` field references the *same* row (the chassis
    "audit-id pre-allocation" pattern).
    """
    return AuditLog(
        id=audit_id,
        occurred_at=datetime.now(UTC),
        operator_sub=operator.sub,
        tenant_id=operator.tenant_id,
        target_id=target_id,
        method=method,
        path=op_id,
        status_code=200,
        request_id=None,
        duration_ms=Decimal("0.00"),
        payload=payload,
    )


def _broadcast_target_id(node: GraphNode) -> uuid.UUID | None:
    """Resolve ``audit_log.target_id`` from the curated edge's ``from`` node.

    Populated iff the from-node is itself a managed target
    (``graph_node.target_id`` non-null). Annotation may reference
    inner-graph nodes (vault-role, k8s-sa) that are not registered
    targets; for those rows the audit / broadcast carry ``target_id =
    None`` per the §10 spec.
    """
    return node.target_id


async def _publish(
    *,
    audit_id: uuid.UUID,
    operator: Operator,
    op_id: str,
    target_name: str | None,
    payload: dict[str, Any],
) -> None:
    """Fail-open broadcast publish.

    Same shape as :func:`refresh._publish_refresh_event` — emits the
    broadcast event and swallows publisher failure so a broken stream
    never rolls back a successful annotate / unannotate.
    """
    try:
        event = BroadcastEvent(
            event_id=uuid.uuid4(),
            ts=datetime.now(UTC),
            tenant_id=operator.tenant_id,
            principal_sub=operator.sub,
            principal_name=operator.name,
            target_name=target_name,
            op_id=op_id,
            op_class=_OP_CLASS,
            result_status="ok",
            audit_id=audit_id,
            payload=payload,
        )
        await publish_event(event)
    except Exception:
        _log.exception(
            "topology_annotation_broadcast_failed",
            op_id=op_id,
            tenant_id=str(operator.tenant_id),
        )


# ---------------------------------------------------------------------------
# Public service functions
# ---------------------------------------------------------------------------


async def annotate_edge(
    session: AsyncSession,
    operator: Operator,
    from_ref: NodeRef,
    kind: str,
    to_ref: NodeRef,
    *,
    note: str | None = None,
    evidence_url: str | None = None,
) -> GraphEdge:
    """Create or refresh a curated ``graph_edge`` row + apply §6 conflicts.

    Resolves the two endpoints in ``operator.tenant_id`` via
    :func:`resolve_node` (#594), validates ``kind`` against
    :class:`GraphEdgeKind` (#593), then:

    * If a row already exists for ``(tenant_id, from_node_id,
      to_node_id, kind)`` (the
      ``graph_edge_tenant_endpoints_kind_idx`` unique index), updates
      ``last_seen`` + ``properties`` and treats the call as
      idempotent.
    * Otherwise inserts a fresh row with ``source='curated'``,
      ``discovered_by=operator.sub``, ``properties`` carrying
      ``note`` / ``evidence_url`` / ``annotated_by`` /
      ``annotated_at``.

    Then runs **§6 conflict detection** (Initiative #364):

    * Same-kind / different-endpoint auto edges are stamped
      ``superseded_by`` (excluded from traversal until the curated
      row is removed).
    * Incompatible-kind edges over the same endpoint pair gain a
      bidirectional ``conflicts_with`` reference.

    Writes one ``audit_log`` row (``op_id='topology.annotate'``,
    ``op_class='write'``) and publishes one broadcast event after
    commit. Broadcast is fail-open per the refresh pattern.

    Args:
        session: Caller-owned :class:`AsyncSession` with **no active
            transaction**. The function opens a ``session.begin()``
            block so the resolve / upsert / conflict scan / audit
            write all commit or roll back together. Mirrors
            :func:`refresh._apply_reconcile`.
        operator: The acting identity. Supplies the tenant scope and
            audit attribution; ``operator.tenant_id`` is the boundary
            both endpoint resolutions and the conflict scan respect.
            Role gating (``tenant_admin``) is the API layer's job
            (T5 #597) — the service trusts its caller.
        from_ref: Operator-supplied :class:`NodeRef` for the
            ``from`` endpoint.
        kind: One of the v0.2 :class:`GraphEdgeKind` values.
            Wrong value raises :class:`InvalidEdgeKindError`.
        to_ref: Operator-supplied :class:`NodeRef` for the
            ``to`` endpoint.
        note: Optional free-text annotation. Stored on
            ``edge.properties['note']``.
        evidence_url: Optional URL the operator attached as evidence
            (e.g. an INVENTORY.md hash). Stored on
            ``edge.properties['evidence_url']``.

    Returns:
        The created or refreshed :class:`GraphEdge` row (post-commit).

    Raises:
        InvalidEdgeKindError: ``kind`` not in
            :class:`GraphEdgeKind`.
        NodeNotFoundError: Either endpoint does not exist in this
            tenant.
        AmbiguousNodeError: An endpoint's name is ambiguous across
            kinds and no ``kind`` was pinned on the
            :class:`NodeRef`.
    """
    async with session.begin():
        plan = await annotate_edge_in_txn(
            session,
            operator,
            from_ref,
            kind,
            to_ref,
            note=note,
            evidence_url=evidence_url,
        )

    await _publish(
        audit_id=plan.audit_id,
        operator=operator,
        op_id=_ANNOTATE_OP_ID,
        target_name=plan.target_name,
        payload=plan.audit_payload,
    )
    return plan.edge


@dataclass(frozen=True, slots=True)
class AnnotatePlan:
    """Result of one :func:`annotate_edge_in_txn` call.

    Carries the row + every post-commit broadcast input the caller needs
    to publish the broadcast event(s) outside the SQL transaction. The
    single-edge wrapper :func:`annotate_edge` publishes one event per
    call; the bulk helper publishes one per row after the batch
    transaction commits, mirroring the same broadcast-after-commit
    discipline.

    Fields:

    * ``edge`` — the upserted :class:`GraphEdge` row (flushed inside the
      caller-owned transaction).
    * ``audit_id`` — pre-allocated id of the ``audit_log`` row written
      for this annotation; broadcast events reference it under
      ``audit_id``.
    * ``audit_payload`` — the shared dict the audit row + the broadcast
      event both carry.
    * ``target_name`` — the from-node's name; surfaces in the broadcast
      event's ``target_name`` field per the chassis convention.
    * ``was_created`` — ``True`` when this call inserted a fresh row,
      ``False`` when it merged onto an existing row (idempotent
      re-annotate or auto→curated promotion). The bulk-import helper
      re-derives its per-row ``create`` / ``update`` / ``conflict``
      classification from this flag plus the post-commit marker arrays
      so intra-batch duplicates (two rows resolving to the same triple
      in one batch) and inter-pass races between validation and apply
      cannot leave the reported counts disagreeing with the committed
      state.
    """

    edge: GraphEdge
    audit_id: uuid.UUID
    audit_payload: dict[str, Any]
    target_name: str
    was_created: bool


async def _insert_curated_edge(
    session: AsyncSession,
    *,
    operator: Operator,
    from_node: GraphNode,
    to_node: GraphNode,
    canonical_kind: str,
    properties: dict[str, Any],
    now: datetime,
) -> GraphEdge:
    """Insert a fresh ``source='curated'`` :class:`GraphEdge` and flush.

    Flushing inside the helper ensures the row has its server-side
    state (``id`` is already Python-side) before the §6 conflict
    scans run -- they may need to read the freshly-inserted row back.
    """
    edge = GraphEdge(
        id=uuid.uuid4(),
        tenant_id=operator.tenant_id,
        from_node_id=from_node.id,
        to_node_id=to_node.id,
        kind=canonical_kind,
        source="curated",
        properties=properties,
        discovered_by=operator.sub,
        first_seen=now,
        last_seen=now,
    )
    session.add(edge)
    await session.flush()
    return edge


def _merge_onto_existing_edge(
    *,
    existing: GraphEdge,
    operator: Operator,
    properties: dict[str, Any],
    now: datetime,
) -> tuple[GraphEdge, dict[str, Any]]:
    """Idempotent upsert path: merge annotation onto an existing edge.

    Covers two cases the single insert/upsert branch must handle:

    1. **Re-annotate of an already-curated row.** Merge new free-text
       fields onto the existing properties; the ``source='curated'``
       and any reciprocal ``conflicts_with`` / ``superseded_by``
       markers are preserved so the re-annotate does not silently
       drop the §6 back-references.

    2. **Annotate over an existing ``source='auto'`` edge with the
       same triple.** The operator's intent is to take ownership of
       that edge going forward — set notes / evidence, run §6
       conflict detection from it, eventually revoke via
       :func:`unannotate_edge`. Leaving the row as ``source='auto'``
       would (a) make the triple-form unannotate raise
       :class:`AutoEdgeDeletionError` so the operator cannot revoke
       their own annotation; (b) let the next refresh's
       :func:`_merge_edge_properties` overwrite the free-text props;
       (c) make :func:`_mark_same_kind_different_endpoint_superseded`
       on a future annotate mis-target the row as another auto
       edge. The promotion to ``source='curated'`` and
       ``discovered_by=operator.sub`` makes the operator the
       canonical author from this call onward.

    Returns ``(edge, pre_mutation_snapshot)`` so the caller can use
    the snapshot as the history row's ``snapshot.before``.
    """
    # Capture the pre-mutation snapshot **before** rewriting
    # ``properties`` / ``source`` so the history row's ``before``
    # reflects the state the operator would have seen on a
    # ``list-edges`` immediately prior to the re-annotate.
    edge_before = edge_snapshot(existing)
    merged = dict(existing.properties or {})
    for key, value in properties.items():
        merged[key] = value
    promoting = existing.source != "curated"
    if promoting:
        # §6 invariant: a curated edge must never carry an
        # ``superseded_by`` marker — that marker is meaningful only
        # on ``source='auto'`` rows that the conflict-detection scan
        # stamped against a different-endpoint curated assertion.
        # When the same triple gets re-annotated, the auto row's
        # stale marker would otherwise persist onto the promoted
        # curated row and hide it from :func:`find_dependents` /
        # :func:`find_dependencies` — the traversal CTE filters
        # ``properties->>'superseded_by' IS NULL``. Drop the key
        # here so the promoted curated edge is immediately visible.
        #
        # ``conflicts_with`` (bidirectional, list-shaped) is kept:
        # the entries point at *other-kind* edges over the same
        # endpoint pair, which still exist and still conflict; the
        # incompatible-kinds scan below re-stamps the curated side
        # without dropping the legitimate back-references.
        merged.pop(_SUPERSEDED_BY, None)
    existing.properties = merged
    existing.last_seen = now
    if promoting:
        existing.source = "curated"
        existing.discovered_by = operator.sub
    return existing, edge_before


def _emit_annotate_history(
    session: AsyncSession,
    *,
    operator: Operator,
    edge: GraphEdge,
    edge_before: dict[str, Any] | None,
    was_created: bool,
    superseded_pairs: list[tuple[GraphEdge, dict[str, Any]]],
    conflict_pairs: list[tuple[GraphEdge, dict[str, Any]]],
    audit_id: uuid.UUID,
    now: datetime,
) -> None:
    """Emit one history row per mutated edge in the annotate transaction.

    Diff-on-write hook (G9.3-T2 #857): one row per mutated edge, all
    sharing this annotate's pre-allocated ``audit_id`` and
    ``valid_from=now`` so the temporal-query verbs (T3 / T4 / T5)
    can reconstruct the operation as a single point-in-time event.
    The curated row's history is emitted with the post-conflict-scan
    snapshot so the ``conflicts_with`` marker is visible in
    ``after`` -- the bidirectional view ``meho topology diff``
    reconstructs.
    """
    edge_change_kind = (
        GraphHistoryChangeKind.CREATED if was_created else GraphHistoryChangeKind.UPDATED
    )
    record_edge_change(
        session,
        edge_id=edge.id,
        tenant_id=operator.tenant_id,
        change_kind=edge_change_kind,
        before=edge_before,
        after=edge_snapshot(edge),
        audit_id=audit_id,
        valid_from=now,
    )
    for other_edge, other_before in (*superseded_pairs, *conflict_pairs):
        # Each marked auto / other-kind edge survives -- the marker
        # write is the only column that changed -- so the history
        # row is ``updated`` with the marker visible in ``after``.
        record_edge_change(
            session,
            edge_id=other_edge.id,
            tenant_id=operator.tenant_id,
            change_kind=GraphHistoryChangeKind.UPDATED,
            before=other_before,
            after=edge_snapshot(other_edge),
            audit_id=audit_id,
            valid_from=now,
        )


async def _upsert_curated_edge(
    session: AsyncSession,
    *,
    operator: Operator,
    from_node: GraphNode,
    to_node: GraphNode,
    canonical_kind: str,
    note: str | None,
    evidence_url: str | None,
    now: datetime,
) -> tuple[GraphEdge, dict[str, Any] | None, bool]:
    """Resolve / upsert the curated edge; return ``(edge, before, was_created)``.

    Wraps the insert-or-merge branch so :func:`annotate_edge_in_txn`
    stays linear. ``before`` is ``None`` for a fresh insert, the
    pre-mutation snapshot for the merge branch.
    """
    properties = {
        "note": note,
        "evidence_url": evidence_url,
        "annotated_by": operator.sub,
        "annotated_at": now.isoformat(),
    }
    existing = await _find_existing_edge(
        session,
        tenant_id=operator.tenant_id,
        from_id=from_node.id,
        to_id=to_node.id,
        kind=canonical_kind,
    )
    if existing is None:
        edge = await _insert_curated_edge(
            session,
            operator=operator,
            from_node=from_node,
            to_node=to_node,
            canonical_kind=canonical_kind,
            properties=properties,
            now=now,
        )
        # ``before`` is None for the curated row's history -- the row
        # did not exist prior to this transaction. ``after`` is captured
        # by :func:`_emit_annotate_history` after both conflict scans
        # have stamped any ``conflicts_with`` markers on this row.
        return edge, None, True
    edge, edge_before = _merge_onto_existing_edge(
        existing=existing,
        operator=operator,
        properties=properties,
        now=now,
    )
    return edge, edge_before, False


async def _run_conflict_scans(
    session: AsyncSession,
    *,
    edge: GraphEdge,
    operator: Operator,
    from_node: GraphNode,
    to_node: GraphNode,
    canonical_kind: str,
) -> tuple[
    list[tuple[GraphEdge, dict[str, Any]]],
    list[tuple[GraphEdge, dict[str, Any]]],
]:
    """Run the §6 same-kind-supersede and incompatible-kinds scans.

    Returns ``(superseded_pairs, conflict_pairs)``. Each pair is
    ``(mutated_edge, pre_mutation_snapshot)`` for the history hook.
    """
    superseded_pairs = await _mark_same_kind_different_endpoint_superseded(
        session,
        tenant_id=operator.tenant_id,
        curated_edge_id=edge.id,
        from_id=from_node.id,
        to_id=to_node.id,
        kind=canonical_kind,
    )
    conflict_pairs = await _mark_incompatible_kinds_conflict(
        session,
        tenant_id=operator.tenant_id,
        curated_edge=edge,
        from_id=from_node.id,
        to_id=to_node.id,
        kind=canonical_kind,
    )
    return superseded_pairs, conflict_pairs


async def annotate_edge_in_txn(
    session: AsyncSession,
    operator: Operator,
    from_ref: NodeRef,
    kind: str,
    to_ref: NodeRef,
    *,
    note: str | None = None,
    evidence_url: str | None = None,
) -> AnnotatePlan:
    """In-transaction body of :func:`annotate_edge` — caller owns the txn.

    The single-edge wrapper :func:`annotate_edge` opens its own
    ``session.begin()`` block around this call and then publishes the
    broadcast event after commit. The bulk-import helper
    :func:`bulk_import_edges` (G9.2-T8 #600) calls this function N times
    inside one shared transaction so the batch is all-or-nothing per
    the issue body's atomicity criterion, then publishes one broadcast
    event per row after the batch commits.

    The function performs the four side effects ``annotate_edge``
    documents (resolve endpoints, idempotent upsert, §6 conflict
    detection, write the audit row) but **does not commit and does not
    publish**. It returns an :class:`AnnotatePlan` so the caller can
    drive the broadcast after its own commit.

    The function expects ``session`` to be inside an active
    transaction. Calling outside a transaction is an error — the
    upsert + conflict-mark sequence must be atomic per row, and the
    function does not open its own ``session.begin()``.

    Args, return shape, and raised errors otherwise match
    :func:`annotate_edge` verbatim.
    """
    canonical_kind = _validate_kind(kind)
    from_node = await resolve_node(session, operator.tenant_id, from_ref.name, from_ref.kind)
    to_node = await resolve_node(session, operator.tenant_id, to_ref.name, to_ref.kind)
    now = datetime.now(UTC)

    edge, edge_before, was_created = await _upsert_curated_edge(
        session,
        operator=operator,
        from_node=from_node,
        to_node=to_node,
        canonical_kind=canonical_kind,
        note=note,
        evidence_url=evidence_url,
        now=now,
    )

    superseded_pairs, conflict_pairs = await _run_conflict_scans(
        session,
        edge=edge,
        operator=operator,
        from_node=from_node,
        to_node=to_node,
        canonical_kind=canonical_kind,
    )

    audit_id = uuid.uuid4()
    payload = _audit_payload(
        op_id=_ANNOTATE_OP_ID,
        from_node=from_node,
        to_node=to_node,
        kind=canonical_kind,
        edge_id=edge.id,
        note=note,
        evidence_url=evidence_url,
        superseded=[pair[0].id for pair in superseded_pairs],
        conflicts=[pair[0].id for pair in conflict_pairs],
    )
    session.add(
        _build_audit_row(
            audit_id=audit_id,
            operator=operator,
            method=_AUDIT_METHOD_ANNOTATE,
            op_id=_ANNOTATE_OP_ID,
            target_id=_broadcast_target_id(from_node),
            payload=payload,
        )
    )
    _emit_annotate_history(
        session,
        operator=operator,
        edge=edge,
        edge_before=edge_before,
        was_created=was_created,
        superseded_pairs=superseded_pairs,
        conflict_pairs=conflict_pairs,
        audit_id=audit_id,
        now=now,
    )

    return AnnotatePlan(
        edge=edge,
        audit_id=audit_id,
        audit_payload=payload,
        target_name=from_node.name,
        was_created=was_created,
    )


def _check_unannotate_selectors(
    *,
    edge_id: uuid.UUID | None,
    from_ref: NodeRef | None,
    kind: str | None,
    to_ref: NodeRef | None,
) -> None:
    """Reject the neither / both / partial-triple selector shapes."""
    has_id = edge_id is not None
    has_triple = from_ref is not None or kind is not None or to_ref is not None
    if has_id == has_triple:
        raise UnannotateSelectorError(
            "unannotate_edge requires exactly one selector: edge_id "
            "OR the full (from_ref, kind, to_ref) triple"
        )
    if has_triple and not (from_ref is not None and kind is not None and to_ref is not None):
        raise UnannotateSelectorError(
            "triple selector requires all three of (from_ref, kind, to_ref)"
        )


async def _resolve_unannotate_target(
    session: AsyncSession,
    operator: Operator,
    *,
    edge_id: uuid.UUID | None,
    from_ref: NodeRef | None,
    kind: str | None,
    to_ref: NodeRef | None,
) -> tuple[GraphEdge, GraphNode, GraphNode]:
    """Resolve the curated edge + its endpoints from either selector form.

    Either ``edge_id`` (primary-key selector) or the ``(from_ref,
    kind, to_ref)`` triple is present; the caller is responsible for
    enforcing exactly-one via :func:`_check_unannotate_selectors`.

    Raises:
        ValueError: target row not found in this tenant or the
            triple form resolves to no row.
        NodeNotFoundError / AmbiguousNodeError: triple-form endpoint
            issues (propagated from :func:`resolve_node`).
        InvalidEdgeKindError: triple-form ``kind`` outside the
            v0.2 enum.
    """
    if edge_id is not None:
        edge = await session.get(GraphEdge, edge_id)
        if edge is None or edge.tenant_id != operator.tenant_id:
            # Tenant boundary: a row in another tenant is
            # indistinguishable from a missing row to the caller.
            raise ValueError(f"graph_edge {edge_id} not found in this tenant")
        from_node = await session.get(GraphNode, edge.from_node_id)
        to_node = await session.get(GraphNode, edge.to_node_id)
        # FK ON DELETE CASCADE protects against orphan rows, but a
        # mid-flight node delete is still possible — treat as not
        # found rather than crashing with a None attribute access.
        if from_node is None or to_node is None:
            raise ValueError(
                f"graph_edge {edge_id} endpoints missing — graph in inconsistent state"
            )
        return edge, from_node, to_node

    assert from_ref is not None  # narrowed for mypy
    assert to_ref is not None
    assert kind is not None
    canonical_kind = _validate_kind(kind)
    from_node = await resolve_node(session, operator.tenant_id, from_ref.name, from_ref.kind)
    to_node = await resolve_node(session, operator.tenant_id, to_ref.name, to_ref.kind)
    edge = await _find_existing_edge(
        session,
        tenant_id=operator.tenant_id,
        from_id=from_node.id,
        to_id=to_node.id,
        kind=canonical_kind,
    )
    if edge is None:
        raise ValueError(
            f"no graph_edge {canonical_kind!r} from {from_ref.name!r} "
            f"to {to_ref.name!r} in this tenant"
        )
    return edge, from_node, to_node


def _emit_unannotate_history(
    session: AsyncSession,
    *,
    operator: Operator,
    removed_id: uuid.UUID,
    removed_edge_snapshot_value: dict[str, Any],
    referencing_befores: list[tuple[GraphEdge, dict[str, Any]]],
    audit_id: uuid.UUID,
    now: datetime,
) -> None:
    """Emit history rows for the unannotate before the live delete flushes.

    Diff-on-write hook (G9.3-T2 #857): emit history rows **before**
    the ``session.delete(edge)`` call so the ``removed`` row's
    ``edge_id`` references a still-live ``graph_edge.id``. The
    ON DELETE SET NULL on the FK kicks in only when the live row is
    hard-deleted; SQLAlchemy may flush the delete before the insert
    if the order is reversed, producing a NULL ``edge_id`` on the
    freshly inserted history row -- defeating the per-resource
    history walk in T3 (the walk filters on ``edge_id``).
    """
    record_edge_change(
        session,
        edge_id=removed_id,
        tenant_id=operator.tenant_id,
        change_kind=GraphHistoryChangeKind.REMOVED,
        before=removed_edge_snapshot_value,
        after=None,
        audit_id=audit_id,
        valid_from=now,
    )
    for ref_edge, ref_before in referencing_befores:
        record_edge_change(
            session,
            edge_id=ref_edge.id,
            tenant_id=operator.tenant_id,
            change_kind=GraphHistoryChangeKind.UPDATED,
            before=ref_before,
            after=edge_snapshot(ref_edge),
            audit_id=audit_id,
            valid_from=now,
        )


async def unannotate_edge(
    session: AsyncSession,
    operator: Operator,
    *,
    edge_id: uuid.UUID | None = None,
    from_ref: NodeRef | None = None,
    kind: str | None = None,
    to_ref: NodeRef | None = None,
) -> uuid.UUID:
    """Hard-delete a curated edge and clear its reciprocal §6 markers.

    Exactly one selector form must be passed (the keyword-only
    signature is what enforces the discipline at the call site):

    * ``edge_id`` — the curated row's primary key.
    * The full ``(from_ref, kind, to_ref)`` triple — resolved to a
      row via the unique ``(tenant_id, from, to, kind)`` index.

    Refuses to delete a row with ``source='auto'`` with
    :class:`AutoEdgeDeletionError` — auto edges resurrect on next
    refresh; manual deletion is meaningless.

    Clears any reciprocal ``superseded_by`` / ``conflicts_with``
    markers the curated row left on auto edges (per §6) so a
    superseded auto edge reappears in traversal after the curated
    assertion is rescinded.

    Writes one ``audit_log`` row (``op_id='topology.unannotate'``,
    ``op_class='write'``) and publishes one broadcast event after
    commit.

    Args:
        session: Caller-owned :class:`AsyncSession` with no active
            transaction; the function opens its own ``session.begin()``.
        operator: Acting identity; supplies the tenant scope + audit
            attribution.
        edge_id: Primary-key selector. Mutually exclusive with the
            triple form.
        from_ref / kind / to_ref: Triple selector. All three must be
            supplied together or none at all.

    Returns:
        The deleted edge's id (the caller may have only had the
        triple form on hand).

    Raises:
        UnannotateSelectorError: Neither or both selector forms
            were supplied — or the triple form is partial.
        NodeNotFoundError: A triple-form endpoint does not exist in
            this tenant.
        AmbiguousNodeError: A triple-form endpoint's name is
            ambiguous and no ``kind`` was pinned on the ``NodeRef``.
        AutoEdgeDeletionError: The targeted row has ``source='auto'``.
        InvalidEdgeKindError: A triple-form ``kind`` is not in
            :class:`GraphEdgeKind`.
        ValueError: The triple form resolves to no row.
    """
    _check_unannotate_selectors(edge_id=edge_id, from_ref=from_ref, kind=kind, to_ref=to_ref)

    async with session.begin():
        removed_id, target_name, audit_id, payload = await _unannotate_in_txn(
            session,
            operator,
            edge_id=edge_id,
            from_ref=from_ref,
            kind=kind,
            to_ref=to_ref,
        )

    await _publish(
        audit_id=audit_id,
        operator=operator,
        op_id=_UNANNOTATE_OP_ID,
        target_name=target_name,
        payload=payload,
    )
    return removed_id


async def _unannotate_in_txn(
    session: AsyncSession,
    operator: Operator,
    *,
    edge_id: uuid.UUID | None,
    from_ref: NodeRef | None,
    kind: str | None,
    to_ref: NodeRef | None,
) -> tuple[uuid.UUID, str, uuid.UUID, dict[str, Any]]:
    """In-transaction body of :func:`unannotate_edge`.

    Resolves the curated row, clears its reciprocal §6 markers, emits
    the diff-on-write history rows, hard-deletes the live row, and
    writes the audit row. Returns ``(removed_id, target_name,
    audit_id, audit_payload)`` so the caller can publish the
    broadcast event after commit.
    """
    edge, from_node, to_node = await _resolve_unannotate_target(
        session,
        operator,
        edge_id=edge_id,
        from_ref=from_ref,
        kind=kind,
        to_ref=to_ref,
    )
    if edge.source != "curated":
        raise AutoEdgeDeletionError(edge.id)

    removed_id = edge.id
    canonical_kind_final = edge.kind

    # Clear reciprocal markers BEFORE deleting the curated row so the
    # SELECT scan still sees the row as a candidate. Capture
    # pre-mutation snapshots so the history rows' ``before`` reflects
    # the supersede / conflict state the operator may have seen on a
    # list-edges; capture the removed curated row's snapshot too --
    # that is the ``before`` for its own ``removed`` history row.
    referencing = await _find_edges_referencing(
        session,
        tenant_id=operator.tenant_id,
        edge_id=removed_id,
    )
    removed_edge_snapshot_value = edge_snapshot(edge)
    now = datetime.now(UTC)
    referencing_befores: list[tuple[GraphEdge, dict[str, Any]]] = [
        (ref_edge, edge_snapshot(ref_edge)) for ref_edge in referencing
    ]
    _clear_reciprocal_markers(referencing, removed_edge_id=removed_id)

    audit_id = uuid.uuid4()
    _emit_unannotate_history(
        session,
        operator=operator,
        removed_id=removed_id,
        removed_edge_snapshot_value=removed_edge_snapshot_value,
        referencing_befores=referencing_befores,
        audit_id=audit_id,
        now=now,
    )

    await session.delete(edge)
    await session.flush()

    payload = _audit_payload(
        op_id=_UNANNOTATE_OP_ID,
        from_node=from_node,
        to_node=to_node,
        kind=canonical_kind_final,
        edge_id=removed_id,
        note=None,
        evidence_url=None,
        superseded=[],
        conflicts=[r.id for r in referencing],
    )
    session.add(
        _build_audit_row(
            audit_id=audit_id,
            operator=operator,
            method=_AUDIT_METHOD_UNANNOTATE,
            op_id=_UNANNOTATE_OP_ID,
            target_id=_broadcast_target_id(from_node),
            payload=payload,
        )
    )
    return removed_id, from_node.name, audit_id, payload
