# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Diff-on-write hook: emit ``*_history`` rows in the live-write transaction.

Initiative #365 (G9.3), Task #857 (T2). The append-only
:class:`~meho_backplane.db.models.GraphNodeHistory` /
:class:`~meho_backplane.db.models.GraphEdgeHistory` tables ship with T1
(#856); this module is the **single shared writer** the refresh +
annotate paths call to populate them.

The atomicity contract is the load-bearing piece:

1. **Same transaction.** Every history row is added to the same
   :class:`AsyncSession` that is currently mutating the live
   ``graph_node`` / ``graph_edge`` rows. A failure anywhere in the
   block raises out of the caller's ``session.begin()`` and rolls
   both the live mutation and the history row back together. The
   live graph and the history table can never disagree about which
   mutations committed.

2. **No own audit row.** History rows reference the *causing*
   operation's :attr:`~meho_backplane.db.models.AuditLog.id` via the
   soft-FK ``audit_id`` column. The refresh service / annotate
   service write one audit row per call; the history rows the hook
   emits link to that single row instead of generating new audit
   rows. Re-emitting an audit row per history row would balloon the
   audit log on a topology refresh that touches dozens of resources
   and would also break the "one operation, one audit row" invariant
   `audit_log` consumers rely on.

3. **Snapshot shape.** Each history row's :attr:`snapshot` JSONB is
   ``{"before": <row-json>|None, "after": <row-json>|None}``:

   * ``created`` -- ``before=None``, ``after=<post-insert row>``.
   * ``updated`` -- ``before=<pre-mutation row>``, ``after=<post-mutation row>``.
   * ``removed`` -- ``before=<final row>``, ``after=None``.

   The bidirectional projection is what makes ``meho topology diff
   <ts1> <ts2>`` (T4 #860) reconstructible without joining back
   against live tables: a tombstone row carries enough state to
   render the removed resource.

Snapshot column selection is **deliberately narrow** (the
``_snapshot_columns`` constant) rather than ``vars(row)`` or
:func:`sqlalchemy.inspect`-based reflection. Two reasons:

* SQLAlchemy ORM attribute access on a soft-deleted or rolled-back
  row can trigger autoflush at the wrong time. Reading exactly the
  columns we want keeps the function side-effect-free.
* Forward-compat: if a future column lands on :class:`GraphNode` /
  :class:`GraphEdge` that should *not* enter the history snapshot
  (e.g. a derived counter), the projection list is the single
  point of opt-in.

Used by:

* :mod:`meho_backplane.topology.refresh` -- one history row per
  diff-applied node and edge inside :func:`_apply_reconcile`.
* :mod:`meho_backplane.topology.annotate` -- one history row per
  annotate / unannotate, plus history rows for the auto edges whose
  ``properties`` got §6 conflict markers stamped on them.
"""

from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import (
    GraphEdge,
    GraphEdgeHistory,
    GraphHistoryChangeKind,
    GraphNode,
    GraphNodeHistory,
)

__all__ = [
    "edge_snapshot",
    "node_snapshot",
    "record_edge_change",
    "record_node_change",
]


#: Columns projected into the ``snapshot.before`` / ``snapshot.after``
#: payload for a :class:`GraphNode`. ``id`` is included so the
#: temporal-query verbs in T3 / T4 / T5 can recover the row identity
#: from a tombstone (the live row is gone, the FK ``node_id`` may have
#: been set NULL by ``ON DELETE SET NULL``). ``tenant_id`` is omitted
#: from the snapshot -- the history row's own ``tenant_id`` column
#: carries it and duplicating it inside the JSONB just wastes bytes.
_NODE_SNAPSHOT_COLUMNS: tuple[str, ...] = (
    "id",
    "kind",
    "name",
    "target_id",
    "properties",
    "discovered_by",
    "first_seen",
    "last_seen",
)

#: Columns projected into the snapshot for a :class:`GraphEdge`. Same
#: rationale as :data:`_NODE_SNAPSHOT_COLUMNS`; ``tenant_id`` is
#: implicit on the history row, ``id`` is preserved for tombstone
#: replay.
_EDGE_SNAPSHOT_COLUMNS: tuple[str, ...] = (
    "id",
    "from_node_id",
    "to_node_id",
    "kind",
    "source",
    "properties",
    "discovered_by",
    "first_seen",
    "last_seen",
)


def _serialise(value: Any) -> Any:
    """JSON-serialise one cell from a :class:`GraphNode` / :class:`GraphEdge`.

    The snapshot JSONB column accepts any JSON-portable payload; we
    coerce the two non-portable types we actually carry (``UUID`` ->
    string, ``datetime`` -> ISO-8601 string) so the row round-trips
    cleanly through both the PG ``jsonb`` and the SQLite ``json`` test
    driver. Plain dicts (``properties``) pass through untouched -- the
    JSON column adapter handles them.
    """
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def node_snapshot(node: GraphNode) -> dict[str, Any]:
    """Project a :class:`GraphNode` row into the snapshot JSONB shape.

    Returns a fresh dict (no aliasing of ``node.properties``) so a
    subsequent mutation of ``node.properties`` in the same transaction
    does not retroactively rewrite a captured snapshot. Used by both
    the refresh diff-on-write hook (to capture ``before`` *before*
    mutating the row) and the annotate hook (to capture ``after``
    *after* the upsert + conflict-marker pass).
    """
    snapshot: dict[str, Any] = {}
    for column in _NODE_SNAPSHOT_COLUMNS:
        raw = getattr(node, column)
        if column == "properties":
            # ``deepcopy`` rather than ``dict()`` so a later in-place edit
            # of a *nested* value inside ``node.properties`` (e.g. a list
            # value reassigned to a new list, or a nested dict updated)
            # does not bleed back into the snapshot. A shallow ``dict()``
            # copies the top-level keys but aliases any container value
            # underneath — fine for the flat ``{note, evidence_url, ...}``
            # shape annotate writes today, but forward-fragile for any
            # future caller that stamps nested structure into properties.
            snapshot[column] = deepcopy(raw or {})
            continue
        snapshot[column] = _serialise(raw)
    return snapshot


def edge_snapshot(edge: GraphEdge) -> dict[str, Any]:
    """Project a :class:`GraphEdge` row into the snapshot JSONB shape.

    Mirror of :func:`node_snapshot` for the edge side -- same
    fresh-dict + properties-deep-copy discipline so a subsequent
    refresh / annotate pass that mutates ``edge.properties`` (the §6
    superseded_by / conflicts_with marker writes) does not invalidate
    a previously captured snapshot.
    """
    snapshot: dict[str, Any] = {}
    for column in _EDGE_SNAPSHOT_COLUMNS:
        raw = getattr(edge, column)
        if column == "properties":
            # ``deepcopy`` rather than ``dict()`` — same forward-compat
            # rationale as :func:`node_snapshot`. Nested list / dict
            # values inside ``properties`` (the §6 ``conflicts_with`` is
            # a list) would otherwise alias the live row's container and
            # any later in-place mutation of that list would
            # retroactively rewrite this captured snapshot.
            snapshot[column] = deepcopy(raw or {})
            continue
        snapshot[column] = _serialise(raw)
    return snapshot


def record_node_change(
    session: AsyncSession,
    *,
    node_id: uuid.UUID,
    tenant_id: uuid.UUID,
    change_kind: GraphHistoryChangeKind,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    audit_id: uuid.UUID,
    valid_from: datetime,
) -> None:
    """Add one :class:`GraphNodeHistory` row to ``session``.

    The row is staged in the caller's transaction -- callers must
    already be inside a ``session.begin()`` block opened by the
    refresh / annotate service. Atomicity is the caller's contract:
    if the surrounding transaction rolls back, this row goes with it.

    ``audit_id`` is the **causing operation's** ``audit_log.id`` --
    the refresh / annotate service pre-allocates it at the top of the
    request and threads it down so every history row from this
    operation links to the same audit row. Re-using one audit row
    per operation (rather than per history row) keeps the audit log
    at one row per operation, which is the contract `audit_log`
    consumers rely on.

    ``valid_from`` is supplied by the caller (rather than being
    Python-defaulted on the model) so every history row from one
    operation carries the **same** timestamp. The diff-on-write
    semantics require this: a refresh that adds 5 nodes in one
    transaction must be queryable as a single point-in-time event,
    not five timestamps separated by microseconds of clock drift.
    """
    session.add(
        GraphNodeHistory(
            node_id=node_id,
            tenant_id=tenant_id,
            change_kind=change_kind.value,
            snapshot={"before": before, "after": after},
            audit_id=audit_id,
            valid_from=valid_from,
        )
    )


def record_edge_change(
    session: AsyncSession,
    *,
    edge_id: uuid.UUID,
    tenant_id: uuid.UUID,
    change_kind: GraphHistoryChangeKind,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    audit_id: uuid.UUID,
    valid_from: datetime,
) -> None:
    """Add one :class:`GraphEdgeHistory` row to ``session``.

    Mirror of :func:`record_node_change` -- same atomicity,
    audit-id-linkage, and shared-timestamp contract; the only
    difference is the row goes into :class:`GraphEdgeHistory` and
    references ``edge_id`` instead of ``node_id``.
    """
    session.add(
        GraphEdgeHistory(
            edge_id=edge_id,
            tenant_id=tenant_id,
            change_kind=change_kind.value,
            snapshot={"before": before, "after": after},
            audit_id=audit_id,
            valid_from=valid_from,
        )
    )
