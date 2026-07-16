# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Batch curated-edge import service (Initiative #364, G9.2-T8 #600).

The single-edge :func:`annotate_edge` flow is the ergonomics floor for
asserting one cross-system relationship; bulk import is what makes
seeding the consumer's prose ``INVENTORY.md`` at onboarding a
single declarative pass rather than a 30-verb shell loop.

**Atomicity contract.** The whole batch lives in **one transaction**.
A validation failure (unknown ``kind``, unresolvable endpoint,
ambiguous endpoint) on any row rolls back the entire transaction —
no partial apply. Re-running the same file is a per-row no-op because
each row's idempotent upsert path matches the underlying
:func:`annotate_edge_in_txn` contract. The chosen semantics (the
issue body offers two options — atomic OR partial with explicit
failure reporting) are atomic-only in v0.2 because:

* The consumer's INVENTORY.md is the source of truth: a partially-
  applied batch leaves the operator with no clean re-run path other
  than "diff what landed vs the file, fix the file, rerun"; an atomic
  failure surfaces the bad row and lets the operator fix-and-retry.
* Bulk import is a v0.2 stretch (Initiative #364 §7); a future widening
  to a ``--continue-on-error`` mode is back-compat and can ship in a
  follow-up.
* The per-row audit + broadcast events still fire — one per applied
  row, fail-open broadcast publish after commit — so a successful
  batch is indistinguishable from N single annotates at the audit /
  event level.

**Dry-run.** Returns the per-row plan (``create`` / ``update`` /
``conflict``) without opening any write transaction. The validation
pass still runs (resolves endpoints, runs ``_find_existing_edge``)
inside a single ``session.begin()`` block that the caller's bulk
import contract makes read-only — no row is added, no row is mutated,
and the transaction always rolls back at the end of the dry-run
block. Acceptance criterion: ``--dry-run`` produces the per-edge plan
and performs zero writes (asserted: row count unchanged, no audit
rows).

**Broadcast.** Per the annotate convention, one event per row, fired
after the batch commits. The fail-open semantics live in the
:func:`annotate._publish` helper — a broken broadcaster never rolls
back a successful batch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import structlog
from sqlalchemy import select

from meho_backplane.db.models import GraphEdge, GraphEdgeKind
from meho_backplane.topology.annotate import (
    _ANNOTATE_OP_ID,
    AnnotatePlan,
    InvalidEdgeKindError,
    NodeRef,
    _find_existing_edge,
    _publish,
    _validate_kind,
    annotate_edge_in_txn,
)
from meho_backplane.topology.resolvers import (
    AmbiguousNodeError,
    NodeNotFoundError,
    resolve_node,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from meho_backplane.auth.operator import Operator

__all__ = [
    "BulkEdgeAction",
    "BulkEdgeResult",
    "BulkImportResult",
    "BulkImportRow",
    "BulkImportRowError",
    "BulkImportValidationError",
    "bulk_import_edges",
]

_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BulkImportRow:
    """One edge to import.

    ``from_ref`` / ``to_ref`` mirror the single-edge :class:`NodeRef`
    pair the :func:`annotate_edge` service takes. ``note`` /
    ``evidence_url`` are the same optional free-text fields. Plain
    immutable dataclass so the REST + CLI fronts have a stable shape
    to coerce their input rows into.
    """

    from_ref: NodeRef
    kind: str
    to_ref: NodeRef
    note: str | None = None
    evidence_url: str | None = None


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


#: Per-row outcome on a successful (or dry-run) batch. ``create`` is a
#: fresh row insert; ``update`` is the idempotent upsert path (the
#: ``(tenant_id, from, to, kind)`` row already existed); ``conflict``
#: surfaces a row that landed with §6 markers attached so the operator
#: sees the recoverability listing the conflict-detection produced.
BulkEdgeAction = Literal["create", "update", "conflict"]


@dataclass(frozen=True, slots=True)
class BulkEdgeResult:
    """Per-row outcome surfaced in :class:`BulkImportResult`.

    ``index`` is the source-order position (zero-based) so a caller
    rendering a YAML file can point at the offending row. ``edge_id``
    is populated post-commit (apply path) and pre-flush (dry-run path
    returns ``None`` — no edge exists yet to point at). ``superseded``
    / ``conflicts`` echo the §6 marker arrays the underlying
    :func:`annotate_edge_in_txn` writes, so a consumer can detect that
    a "successful" import nonetheless rewrote the auto-edge landscape.
    """

    index: int
    action: BulkEdgeAction
    edge_id: str | None
    from_name: str
    from_kind: str
    to_name: str
    to_kind: str
    kind: str
    superseded: list[str]
    conflicts: list[str]


@dataclass(frozen=True, slots=True)
class BulkImportResult:
    """Aggregate outcome of one batch.

    ``dry_run`` echoes the call-site flag so callers (REST handler,
    CLI renderer) can branch on it without reading the request side.
    Counts are derived but pre-computed so the JSON shape is stable
    for the operator's eye (the consumer's onboarding doc renders
    them).
    """

    dry_run: bool
    created: int
    updated: int
    conflicts: int
    rows: list[BulkEdgeResult]


# ---------------------------------------------------------------------------
# Errors (HTTP-agnostic; API layer maps to status codes)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BulkImportRowError:
    """One row's validation failure, attached to a
    :class:`BulkImportValidationError`.

    ``index`` is the source-order position; ``error`` is a short
    machine-readable token (``invalid_kind`` / ``node_not_found`` /
    ``ambiguous_node``); ``message`` is the human-readable diagnostic
    the front layer renders. The shape mirrors the per-row error
    envelopes the REST layer surfaces — one struct per failure rather
    than a free-form string blob, so the CLI / MCP fronts can render
    a structured "row 3: invalid_kind 'made-up'" list.
    """

    index: int
    error: str
    message: str
    name: str | None = None
    kind: str | None = None
    kinds: list[str] | None = None


class BulkImportValidationError(ValueError):
    """Aggregate validation failure for one or more rows.

    Raised after the validation pass collects every row's error — the
    batch is rejected atomically, but the operator sees **all** the
    problems in one shot rather than fixing one and rediscovering the
    next on the rerun. The REST layer maps this to a 422 with the
    structured ``errors`` array in ``detail``.
    """

    def __init__(self, errors: list[BulkImportRowError]) -> None:
        self.errors = errors
        super().__init__(f"bulk import rejected: {len(errors)} row(s) failed validation")


# ---------------------------------------------------------------------------
# Public service
# ---------------------------------------------------------------------------


async def bulk_import_edges(
    session: AsyncSession,
    operator: Operator,
    rows: list[BulkImportRow],
    *,
    dry_run: bool = False,
) -> BulkImportResult:
    """Annotate every row in ``rows`` in one transaction (or plan only).

    The function performs two passes:

    1. **Validation pass.** Resolves both endpoints of every row,
       validates the ``kind`` against the open slug grammar
       (:data:`~meho_backplane.db.models.KIND_SLUG_PATTERN`), and
       records what the apply pass would do (``create`` for a row that
       does not yet exist; ``update`` for a row that matches an
       existing ``(tenant, from, to, kind)``; ``conflict`` for a row
       whose endpoint pair already carries a §6 conflict marker).
       Failure on any row aggregates into a single
       :class:`BulkImportValidationError` carrying every row's
       per-row :class:`BulkImportRowError`.

       The pass runs inside a transaction so the
       :func:`resolve_node` lookups + :func:`_find_existing_edge`
       reads see a consistent snapshot. In dry-run mode the
       transaction is always rolled back at the end of the block
       — no row is mutated, no audit row is inserted.

    2. **Apply pass.** Skipped under ``dry_run=True``. Otherwise opens
       a single ``session.begin()`` block, calls
       :func:`annotate_edge_in_txn` for each row inside it, and
       commits. Post-commit publishes one broadcast event per row
       via the shared fail-open helper. Per-row audit rows are
       written inside the transaction by ``annotate_edge_in_txn``,
       so the audit + broadcast count matches the issue criterion:
       one audit row per edge + one broadcast event per edge.

    Args:
        session: Caller-owned :class:`AsyncSession` with **no active
            transaction**. The function opens its own
            ``session.begin()`` block (twice — once for validation,
            once for apply); reusing a session that already has a
            txn-in-flight is a programming error.
        operator: The acting principal. Supplies the tenant scope
            (every endpoint resolution + every edge upsert respects
            ``operator.tenant_id``) and the audit attribution.
        rows: Source-ordered list of :class:`BulkImportRow`. Empty
            list is a no-op (returns a zero-row result; no
            validation error). The caller is free to dedupe inputs;
            this function does not.
        dry_run: When true, the apply pass is skipped. The returned
            :class:`BulkImportResult` echoes the plan with
            ``edge_id=None`` on every row; no audit row is written,
            no broadcast event is published.

    Returns:
        A :class:`BulkImportResult` carrying the per-row outcomes +
        the aggregate counts.

    Raises:
        BulkImportValidationError: One or more rows failed validation
            (invalid ``kind``, missing endpoint, ambiguous endpoint).
            The error carries every row's failure; partial rejection
            is the deliberate semantics — a malformed batch is never
            partially applied.
    """
    if not rows:
        return BulkImportResult(dry_run=dry_run, created=0, updated=0, conflicts=0, rows=[])

    # ----- Pass 1: validation in a read-only transaction --------------------
    #
    # We open a single ``session.begin()`` block so every resolver
    # lookup + every existing-edge probe sees a consistent snapshot.
    # The pass either succeeds (apply pass is allowed to proceed) or
    # fails atomically — no partial annotate ever leaks past this
    # check. The dry-run path also exits through this branch (with
    # the transaction rolled back via the ``raise`` path on the
    # else-clause when ``dry_run`` is true; see below).

    errors: list[BulkImportRowError] = []
    plan_rows: list[BulkEdgeResult] = []

    async with session.begin():
        for index, row in enumerate(rows):
            try:
                action, edge_summary = await _classify_row(session, operator, index, row)
            except _RowValidationError as exc:
                errors.append(exc.error)
                continue
            plan_rows.append(
                BulkEdgeResult(
                    index=index,
                    action=action,
                    edge_id=edge_summary.edge_id,
                    from_name=edge_summary.from_name,
                    from_kind=edge_summary.from_kind,
                    to_name=edge_summary.to_name,
                    to_kind=edge_summary.to_kind,
                    kind=edge_summary.kind,
                    superseded=edge_summary.superseded,
                    conflicts=edge_summary.conflicts,
                )
            )

    if errors:
        raise BulkImportValidationError(errors)

    counts = _aggregate_counts(plan_rows)
    if dry_run:
        return BulkImportResult(
            dry_run=True,
            created=counts.created,
            updated=counts.updated,
            conflicts=counts.conflicts,
            rows=plan_rows,
        )

    # ----- Pass 2: apply — one transaction wrapping every annotate ----------

    plans: list[AnnotatePlan] = []
    async with session.begin():
        for row in rows:
            plan = await annotate_edge_in_txn(
                session,
                operator,
                row.from_ref,
                row.kind,
                row.to_ref,
                note=row.note,
                evidence_url=row.evidence_url,
            )
            plans.append(plan)

    # Publish one broadcast event per row after commit. The helper is
    # fail-open per the refresh pattern — a broken broadcaster never
    # rolls back the successful batch.
    for plan in plans:
        await _publish(
            audit_id=plan.audit_id,
            operator=operator,
            op_id=_ANNOTATE_OP_ID,
            target_name=plan.target_name,
            payload=plan.audit_payload,
        )

    # Re-build the row list with post-commit edge ids so the response
    # can carry the actually-applied row state (the validation-pass
    # plan_rows have stale ids — the upsert path on update may have
    # mutated the existing row, the create path adds a new id we now
    # know).
    applied_rows = _materialise_apply_rows(plans, plan_rows)
    apply_counts = _aggregate_counts(applied_rows)
    _log.info(
        "topology_bulk_import_committed",
        operator_sub=operator.sub,
        tenant_id=str(operator.tenant_id),
        created=apply_counts.created,
        updated=apply_counts.updated,
        conflicts=apply_counts.conflicts,
        total=len(applied_rows),
    )
    return BulkImportResult(
        dry_run=False,
        created=apply_counts.created,
        updated=apply_counts.updated,
        conflicts=apply_counts.conflicts,
        rows=applied_rows,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Counts:
    created: int
    updated: int
    conflicts: int


def _aggregate_counts(rows: list[BulkEdgeResult]) -> _Counts:
    """Tally action buckets for the result envelope."""
    created = sum(1 for r in rows if r.action == "create")
    updated = sum(1 for r in rows if r.action == "update")
    conflicts = sum(1 for r in rows if r.action == "conflict")
    return _Counts(created=created, updated=updated, conflicts=conflicts)


@dataclass(frozen=True, slots=True)
class _EdgeSummary:
    """Mid-classify holder so the per-row record stays a single struct."""

    edge_id: str | None
    from_name: str
    from_kind: str
    to_name: str
    to_kind: str
    kind: str
    superseded: list[str]
    conflicts: list[str]


class _RowValidationError(Exception):
    """Internal sentinel: the row failed validation; carries the error."""

    def __init__(self, error: BulkImportRowError) -> None:
        self.error = error
        super().__init__(error.message)


async def _classify_row(
    session: AsyncSession,
    operator: Operator,
    index: int,
    row: BulkImportRow,
) -> tuple[BulkEdgeAction, _EdgeSummary]:
    """Resolve endpoints + kind + existing-row state for one input row.

    Returns the planned action (``create`` / ``update`` / ``conflict``)
    + a summary the result envelope renders. The ``conflict``
    classification is conservative: a pre-existing edge over the same
    endpoint pair that already carries §6 markers (``conflicts_with``
    or ``superseded_by``) routes through ``conflict`` so the operator
    sees the recoverability listing alongside the apply plan. A row
    that fires *new* §6 markers via the conflict-detection scan still
    classifies as ``create``/``update`` here — the marker arrays carry
    the diagnostic the operator needs.

    Raises:
        _RowValidationError: Any of the three validation surfaces
            failed for this row.
    """
    # Validate kind first — fail-fast on a malformed slug before doing
    # any node-resolution IO. `kinds` carries the well-known set as
    # suggestions (the vocabulary is open; membership is not enforced).
    try:
        canonical_kind = _validate_kind(row.kind)
    except InvalidEdgeKindError as exc:
        valid = sorted(k.value for k in GraphEdgeKind)
        raise _RowValidationError(
            BulkImportRowError(
                index=index,
                error="invalid_kind",
                message=str(exc),
                kind=exc.kind,
                kinds=valid,
            )
        ) from exc

    try:
        from_node = await resolve_node(
            session, operator.tenant_id, row.from_ref.name, row.from_ref.kind
        )
    except NodeNotFoundError as exc:
        raise _RowValidationError(
            BulkImportRowError(
                index=index,
                error="node_not_found",
                message=str(exc),
                name=exc.name,
                kind=exc.kind,
            )
        ) from exc
    except AmbiguousNodeError as exc:
        raise _RowValidationError(
            BulkImportRowError(
                index=index,
                error="ambiguous_node",
                message=str(exc),
                name=exc.name,
                kinds=sorted(exc.kinds),
            )
        ) from exc

    try:
        to_node = await resolve_node(session, operator.tenant_id, row.to_ref.name, row.to_ref.kind)
    except NodeNotFoundError as exc:
        raise _RowValidationError(
            BulkImportRowError(
                index=index,
                error="node_not_found",
                message=str(exc),
                name=exc.name,
                kind=exc.kind,
            )
        ) from exc
    except AmbiguousNodeError as exc:
        raise _RowValidationError(
            BulkImportRowError(
                index=index,
                error="ambiguous_node",
                message=str(exc),
                name=exc.name,
                kinds=sorted(exc.kinds),
            )
        ) from exc

    existing = await _find_existing_edge(
        session,
        tenant_id=operator.tenant_id,
        from_id=from_node.id,
        to_id=to_node.id,
        kind=canonical_kind,
    )

    action: BulkEdgeAction
    edge_id: str | None
    superseded: list[str] = []
    conflicts: list[str] = []
    if existing is None:
        action = "create"
        edge_id = None
    else:
        edge_id = str(existing.id)
        existing_props = existing.properties or {}
        raw_conflicts = existing_props.get("conflicts_with")
        if isinstance(raw_conflicts, list):
            conflicts = [str(c) for c in raw_conflicts]
        if existing_props.get("superseded_by"):
            superseded = [str(existing_props["superseded_by"])]
        # A pre-existing edge that already carries §6 markers gets the
        # ``conflict`` classification so the operator sees it in the
        # plan; a clean re-annotate gets ``update``.
        action = "conflict" if (conflicts or superseded) else "update"

    # Same-kind / different-endpoint scan: are there auto edges this
    # apply would supersede? Reads only; the actual marker write
    # happens inside the apply pass.
    incoming_supersedes = await _scan_would_supersede(
        session, operator, from_node.id, to_node.id, canonical_kind
    )
    if incoming_supersedes:
        superseded = list({*superseded, *incoming_supersedes})
        if action == "create":
            # A row that creates a curated edge AND supersedes an auto
            # edge surfaces as ``conflict`` so the recoverability
            # listing flags it pre-apply.
            action = "conflict"

    # Incompatible-kind / same-endpoint scan: §6 class 2. The apply
    # pass's :func:`annotate._mark_incompatible_kinds_conflict` will
    # stamp bidirectional ``conflicts_with`` markers; pre-apply we
    # surface the row as ``conflict`` so the dry-run plan exposes
    # the recoverability listing for *both* §6 classes (the
    # same-kind / different-endpoint case above and the
    # incompatible-kind / same-endpoint case here). Reads only;
    # marker writes happen in the apply transaction.
    incoming_conflict_kinds = await _scan_would_conflict_kinds(
        session, operator, from_node.id, to_node.id, canonical_kind
    )
    if incoming_conflict_kinds:
        conflicts = list({*conflicts, *incoming_conflict_kinds})
        action = "conflict"

    return action, _EdgeSummary(
        edge_id=edge_id,
        from_name=from_node.name,
        from_kind=from_node.kind,
        to_name=to_node.name,
        to_kind=to_node.kind,
        kind=canonical_kind,
        superseded=superseded,
        conflicts=conflicts,
    )


async def _scan_would_supersede(
    session: AsyncSession,
    operator: Operator,
    from_id: object,
    to_id: object,
    kind: str,
) -> list[str]:
    """Read-only counterpart of
    :func:`annotate._mark_same_kind_different_endpoint_superseded`.

    Returns the ids of auto edges the apply pass would stamp
    ``superseded_by`` for this row. Reads only; the apply pass's
    marker write is what makes the supersede effective.
    """
    stmt = select(GraphEdge).where(
        GraphEdge.tenant_id == operator.tenant_id,
        GraphEdge.from_node_id == from_id,
        GraphEdge.kind == kind,
        GraphEdge.source == "auto",
        GraphEdge.to_node_id != to_id,
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [str(r.id) for r in rows]


async def _scan_would_conflict_kinds(
    session: AsyncSession,
    operator: Operator,
    from_id: object,
    to_id: object,
    kind: str,
) -> list[str]:
    """Read-only counterpart of
    :func:`annotate._mark_incompatible_kinds_conflict`.

    Returns the ids of edges that already exist over the same endpoint
    pair with a *different* ``kind`` — the §6 class-2 (incompatible-kind
    / same-endpoint) conflict signal. Reads only; the apply pass's
    bidirectional ``conflicts_with`` marker write is what makes the
    conflict effective in the audit / broadcast payloads.

    The query mirrors :func:`annotate._mark_incompatible_kinds_conflict`'s
    SELECT verbatim so the plan-pass classification and the apply-pass
    marker write target the same row set; a divergence between the two
    would let dry-run plans hide §6 conflicts the apply would surface.
    """
    stmt = select(GraphEdge).where(
        GraphEdge.tenant_id == operator.tenant_id,
        GraphEdge.from_node_id == from_id,
        GraphEdge.to_node_id == to_id,
        GraphEdge.kind != kind,
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [str(r.id) for r in rows]


def _materialise_apply_rows(
    plans: list[AnnotatePlan],
    plan_rows: list[BulkEdgeResult],
) -> list[BulkEdgeResult]:
    """Rebuild per-row results with post-commit edge ids + final markers.

    The validation pass produced ``plan_rows`` keyed by source index;
    the apply pass produced :class:`AnnotatePlan` objects in the same
    order. The plan ids point at the freshly-flushed (or refreshed)
    rows, and the audit payload carries the final ``superseded`` /
    ``conflicts`` lists post-marker-write — so we re-pack the result
    using those authoritative numbers.

    The per-row ``action`` is re-derived from the apply pass's actual
    insert-vs-merge outcome (:attr:`AnnotatePlan.was_created`) plus the
    post-commit marker arrays, **not** carried forward from the pass-1
    plan. The pass-1 ``action`` is a *prediction* that two situations
    can invalidate:

    1. **Intra-batch duplicates.** Two rows in the same batch resolving
       to the same ``(from, to, kind)`` triple — pass-1 saw the row as
       missing for both rows (the first row was not yet flushed); pass-2
       inserts on row N and merges on row N+1. Carrying pass-1's action
       forward would count both as ``create`` and report 2 created
       edges when the DB only holds 1.

    2. **Inter-pass races.** A concurrent transaction inserted the row
       between pass-1 and pass-2. Pass-1 planned ``create``; pass-2 got
       a unique-constraint hit on flush, then the second annotate
       merged onto the racing row. Pass-1's action no longer reflects
       what actually happened.

    Re-deriving from the apply outcome makes the reported counts match
    the committed DB state by construction. Any post-commit marker
    array — ``superseded`` (§6 class 1, same-kind/different-endpoint)
    or ``conflicts`` (§6 class 2, incompatible-kind/same-endpoint) —
    forces ``action='conflict'`` so the recoverability listing
    surfaces; otherwise ``was_created`` distinguishes ``create``
    (fresh insert) from ``update`` (idempotent merge).
    """
    out: list[BulkEdgeResult] = []
    for plan_row, applied in zip(plan_rows, plans, strict=True):
        payload = applied.audit_payload
        edge = applied.edge
        superseded = [str(s) for s in payload.get("superseded", [])]
        conflicts = [str(c) for c in payload.get("conflicts", [])]
        action: BulkEdgeAction
        if superseded or conflicts:
            action = "conflict"
        elif applied.was_created:
            action = "create"
        else:
            action = "update"
        out.append(
            BulkEdgeResult(
                index=plan_row.index,
                action=action,
                edge_id=str(edge.id),
                from_name=plan_row.from_name,
                from_kind=plan_row.from_kind,
                to_name=plan_row.to_name,
                to_kind=plan_row.to_kind,
                kind=plan_row.kind,
                superseded=superseded,
                conflicts=conflicts,
            )
        )
    return out
