# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pydantic models for topology query inputs and result shapes.

Task #451 (G9.1-T4). The three traversal verbs in
:mod:`meho_backplane.topology.query` return these frozen models. The
shapes mirror the immutability discipline the connector
:class:`~meho_backplane.connectors.schemas.NodeHint` family already
established: ``properties`` round-trips as a plain ``dict`` over the
wire but is wrapped in :class:`types.MappingProxyType` after validation
so a frozen :class:`TopologyNode` is deeply immutable end to end. A
caller cannot mutate a node's ``properties`` bag and have that leak
back into a shared result list.

Task #596 (G9.2-T4) adds :class:`TopologyEdgeEndpoint` and
:class:`TopologyEdge` for the flat edge-listing helper
:func:`meho_backplane.topology.query.list_edges`. The edge shape re-uses
the same deep-freeze discipline on ``properties`` so the conflict
markers ``properties.conflicts_with`` (a JSONB array, written by
G9.2-T3 #595) and ``properties.superseded_by`` (a UUID, also written by
#595) cannot be mutated by a caller and leak back into shared state —
important because the marker list is the recoverability surface for a
wrong annotation.
"""

from __future__ import annotations

from datetime import datetime
from types import MappingProxyType
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

__all__ = [
    "TopologyDiffEntry",
    "TopologyDiffResult",
    "TopologyEdge",
    "TopologyEdgeEndpoint",
    "TopologyNode",
    "TopologyPath",
    "TopologyTimelineEntry",
    "TopologyTimelineResult",
]


def _deep_freeze(value: Any) -> Any:
    """Recursively make a JSON-shaped value immutable.

    ``dict`` → :class:`types.MappingProxyType` (read-only view), ``list``
    → ``tuple``, every primitive returned unchanged. Applied to
    ``properties`` so a frozen :class:`TopologyNode` is immutable all the
    way down — a caller cannot reach into a nested bag and mutate shared
    result state. The inverse is :func:`_deep_thaw`.
    """
    if isinstance(value, dict):
        return MappingProxyType({k: _deep_freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(v) for v in value)
    return value


def _deep_thaw(value: Any) -> Any:
    """Inverse of :func:`_deep_freeze` for serialisation.

    ``MappingProxyType`` → plain ``dict``, ``tuple`` → ``list``,
    primitives unchanged, so ``model_dump`` / ``model_dump_json`` emit a
    plain mutable JSON object rather than leaking the internal frozen
    representation over the wire.
    """
    if isinstance(value, MappingProxyType):
        return {k: _deep_thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw(v) for v in value]
    return value


class TopologyNode(BaseModel):
    """One ``graph_node`` row reached during a traversal.

    ``depth`` is the distance from the query root: the root itself is
    depth ``0``, its immediate dependents/dependencies are depth ``1``,
    transitive ones depth ``2``, and so on. ``via_edge_kind`` is the
    ``graph_edge.kind`` of the edge used to reach this node, or
    ``None`` for the root (which is reached by no edge).
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    kind: str
    name: str
    properties: dict[str, Any] = Field(default_factory=dict)
    depth: int
    via_edge_kind: str | None

    @model_validator(mode="after")
    def _freeze_properties(self) -> TopologyNode:
        object.__setattr__(self, "properties", _deep_freeze(dict(self.properties)))
        return self

    @field_serializer("properties")
    def _serialize_properties(self, value: dict[str, Any]) -> dict[str, Any]:
        # `value` is always the top-level frozen mapping, so the thawed
        # result is always a plain dict; the cast narrows _deep_thaw's
        # intentionally-broad return for the field-serialiser contract.
        thawed: dict[str, Any] = _deep_thaw(value)
        return thawed


class TopologyPath(BaseModel):
    """An ordered shortest path between two nodes.

    ``nodes`` runs from the ``from`` node (``depth == 0``) to the
    ``to`` node (``depth == total_hops``) inclusive. ``total_hops`` is
    the number of edges traversed, i.e. ``len(nodes) - 1``. v0.2 is
    unweighted: every edge costs one hop.
    """

    model_config = ConfigDict(frozen=True)

    nodes: tuple[TopologyNode, ...] = Field(min_length=1)
    total_hops: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_hops_match_nodes(self) -> TopologyPath:
        expected = len(self.nodes) - 1
        if self.total_hops != expected:
            raise ValueError(
                f"total_hops ({self.total_hops}) must equal len(nodes) - 1 ({expected})"
            )
        return self


class TopologyEdgeEndpoint(BaseModel):
    """One endpoint of a :class:`TopologyEdge` — the ``from`` or ``to`` node.

    Compact node identity for the flat edge-listing helper. Carries the
    three fields a human-readable edge summary needs: the node ``id``
    (caller may follow it back to the full :class:`TopologyNode`), the
    ``kind`` (the closed enum from migration ``0007``), and the
    ``name`` (unique within ``(tenant_id, kind)``). The full node
    ``properties`` bag is intentionally **not** included — an edge
    listing is a survey of relationships, not a node dump; callers that
    need the bag look the node up separately via
    :func:`meho_backplane.topology.resolvers.resolve_node`.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    kind: str
    name: str


class TopologyEdge(BaseModel):
    """One ``graph_edge`` row returned by :func:`list_edges`.

    Flat edge summary (no traversal context — there is no ``depth`` or
    ``via_edge_kind``; those concepts only mean something during a
    walk). The frozen Pydantic shape mirrors the immutability discipline
    of :class:`TopologyNode`: ``properties`` is deep-frozen so the
    conflict-marker arrays (``conflicts_with``) and the supersede UUID
    (``superseded_by``) — both written by G9.2-T3 (#595) and read by
    this helper's ``conflicts_only=True`` filter — cannot be mutated by
    a caller and leak back into shared result state.

    ``last_seen`` is the refresh service's "I observed this edge at"
    timestamp (NULL after a soft-delete; soft-deleted edges are
    excluded from :func:`list_edges` by default). It doubles as the
    stable total-order key the helper paginates against:
    ``ORDER BY last_seen DESC NULLS LAST, id`` is total because ``id``
    is a UUID primary key, and ``DESC`` puts the most-recently-observed
    edges first — the order an operator scanning a fresh inventory
    expects.
    """

    # ``from`` / ``to`` are Python keywords; the attribute names are
    # ``from_endpoint`` / ``to_endpoint`` so kwargs / mypy stay clean.
    # The wire shape (``from`` / ``to``, per Initiative #364 §8) is
    # restored by ``serialization_alias`` on each field: FastAPI emits
    # the model with ``model_dump(by_alias=True)`` by default for
    # response models, so the JSON keys land as the issue body specifies
    # without coupling every route handler to a manual ``by_alias=True``
    # dump. ``populate_by_name=True`` lets the construct-time kwargs
    # accept both the attribute name and the alias — important for
    # in-process callers (tests, MCP fronts) that construct instances
    # directly with Python identifiers.
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    id: UUID
    from_endpoint: TopologyEdgeEndpoint = Field(serialization_alias="from")
    to_endpoint: TopologyEdgeEndpoint = Field(serialization_alias="to")
    kind: str
    source: str
    properties: dict[str, Any] = Field(default_factory=dict)
    last_seen: datetime | None

    @model_validator(mode="after")
    def _freeze_properties(self) -> TopologyEdge:
        object.__setattr__(self, "properties", _deep_freeze(dict(self.properties)))
        return self

    @field_serializer("properties")
    def _serialize_properties(self, value: dict[str, Any]) -> dict[str, Any]:
        # `value` is always the top-level frozen mapping; the cast
        # narrows _deep_thaw's intentionally-broad return for the
        # field-serialiser contract.
        thawed: dict[str, Any] = _deep_thaw(value)
        return thawed


class TopologyTimelineEntry(BaseModel):
    """One row of the topology timeline (G9.3-T5).

    Task #861 ships the tenant-wide chronological feed of graph
    changes -- "what's been happening in the graph in the last hour"
    without rooting at a specific resource. Each row projects one
    ``graph_node_history`` or ``graph_edge_history`` mutation into a
    compact summary the CLI / REST / MCP fronts can render or page
    through.

    Field-to-column mapping:

    * ``valid_from`` -- ``*_history.valid_from``; every history row
      from one operation carries the same timestamp (the diff-on-write
      hook is the single source per :mod:`.history`).
    * ``history_id`` -- ``*_history.history_id``. Required because the
      cursor's tie-breaker discriminator is ``(valid_from, history_id,
      source)`` -- ``valid_from`` alone is not unique within a refresh.
    * ``source`` -- ``"node"`` for :class:`GraphNodeHistory`, ``"edge"``
      for :class:`GraphEdgeHistory`. The discriminator the UNION
      preserves.
    * ``change_kind`` -- one of ``"created"`` / ``"updated"`` /
      ``"removed"`` per :class:`GraphHistoryChangeKind`.
    * ``resource_id`` -- ``node_id`` or ``edge_id`` of the mutated row.
      ``None`` only after the referenced live row has been
      hard-deleted (``ON DELETE SET NULL``) -- a rare survivability
      shape; most timeline rows carry the FK intact.
    * ``summary`` -- one-line human-readable description derived from
      ``snapshot.before`` / ``snapshot.after``. Format:
      ``"<change_kind> <kind> <name>"`` for nodes (e.g. ``"created vm
      vm-prod"``); ``"<change_kind> <edge_kind> <from> -> <to>"`` for
      edges (e.g. ``"removed runs-on vm-prod -> host-a"``). Falls back
      to ``"<change_kind> <source>"`` when the live row is gone and
      the snapshot did not preserve enough context.
    * ``audit_id`` -- the soft-FK to the ``audit_log.id`` of the
      operation that caused the mutation. ``None`` only for legacy
      / mid-rollout rows; the diff-on-write hook always populates it.
    """

    model_config = ConfigDict(frozen=True)

    valid_from: datetime
    history_id: int
    source: str
    change_kind: str
    resource_id: UUID | None
    summary: str
    audit_id: UUID | None


class TopologyTimelineResult(BaseModel):
    """Page of timeline rows plus the forward-only continuation cursor.

    ``next_cursor`` is :data:`None` when fewer than ``limit`` rows
    were available (the query reached the end of the matching set).
    Consumers iterate by re-issuing the same filter with
    ``cursor = next_cursor`` until ``next_cursor`` is None.

    The cursor is opaque (base64-encoded JSON over ``(valid_from,
    history_id, source)``) -- consumers treat it as a token. Stability
    under concurrent inserts: a new history row landing in the window
    between page N and page N+1 is naturally placed by the keyset
    compare (``(valid_from, history_id) < (cursor.ts, cursor.id)``)
    and either appears on a later page (if it falls below the cursor)
    or never (if it lands above the cursor). No row is duplicated or
    skipped by the act of paging.
    """

    model_config = ConfigDict(frozen=True)

    # ``tuple`` (not ``list``) so the ``frozen=True`` immutability
    # contract extends to in-place mutation: a list would still accept
    # ``.append`` / ``.pop`` despite the ``frozen`` block on attribute
    # reassignment. Consumers iterating over ``rows`` see the same shape.
    rows: tuple[TopologyTimelineEntry, ...]
    next_cursor: str | None


class TopologyDiffEntry(BaseModel):
    """One resource's net delta between ``ts1`` and ``ts2`` (G9.3-T4 #860).

    Task #860 ships the graph-level *diff* between two timestamps. Each
    entry summarises **one node or edge** that mutated in the window:

    * ``change_kind`` is the **net** change for the resource in
      ``(ts1, ts2]``. The fold rule per resource:

      - ``created`` -- the first history row in window is ``created``
        and the last in-window row is not ``removed``.
      - ``removed`` -- the last in-window row is ``removed``.
      - ``updated`` -- the resource existed before ``ts1`` and has at
        least one in-window mutation.

      A resource ``created`` *and* ``removed`` inside the same window
      nets to ``removed`` (the post-window state is "gone"). This is
      the operator's mental model for "what changed since ts1?".

    * ``source`` -- ``"node"`` or ``"edge"`` (which history table the
      resource was projected from).
    * ``resource_id`` -- ``graph_node.id`` or ``graph_edge.id`` of the
      mutated resource. ``None`` only when the live row has been
      hard-deleted post-window via ``ON DELETE SET NULL``; rare.
    * ``kind`` -- the resource's domain ``kind`` (``vm``, ``host``,
      ``runs-on``, ``mounts``, ...). Picked from the post-state for
      ``created``/``updated`` and from the pre-state for ``removed``
      so the entry always carries a renderable kind even for
      tombstones.
    * ``name`` -- ``graph_node.name`` for nodes; ``None`` for edges
      (the edge snapshot does not carry endpoint names -- the snapshot
      stores FKs, not names; the CLI uses ``--json`` + a separate node
      lookup if the operator wants full endpoint detail).
    * ``summary`` -- one-line human-readable description, format
      ``"<change_kind> <source> <kind> <name>"`` for nodes and
      ``"<change_kind> <source> <kind>"`` for edges, matching the
      timeline-entry summary style.
    """

    model_config = ConfigDict(frozen=True)

    change_kind: str
    source: str
    resource_id: UUID | None
    kind: str
    name: str | None
    summary: str


class TopologyDiffResult(BaseModel):
    """Result of :func:`query_diff` -- the diff entries plus a truncation flag.

    The v0.2 diff surface enforces a **1000-row hard cap** (the substrate
    cap, mirrored by the CLI / REST / MCP fronts). When the seeded
    cohort of changed resources exceeds the cap, the result is truncated
    at 1000 entries and ``truncated`` flips to ``True``; ``truncation_hint``
    carries the canonical operator-facing remediation line. Callers
    rendering a structured summary should surface the hint verbatim so
    every front shows the same "narrow the time window" recovery path.

    Aggregate counts (``created`` / ``updated`` / ``removed``) are
    derived from ``entries`` -- both the substrate result and the
    fronts compute the totals from the same source so a UI re-rendering
    the JSON can rely on the counts matching the entries.

    Ordering: entries are returned in the same insertion order the
    substrate folds them in -- ``(source, resource_id)`` after a
    per-resource fold. Consumers wanting a presentation order
    (``removed`` first, then ``updated``, then ``created``) sort
    client-side; the substrate stays out of presentation.
    """

    model_config = ConfigDict(frozen=True)

    entries: tuple[TopologyDiffEntry, ...]
    truncated: bool
    truncation_hint: str | None
