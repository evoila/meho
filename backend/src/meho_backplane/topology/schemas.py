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

__all__ = ["TopologyEdge", "TopologyEdgeEndpoint", "TopologyNode", "TopologyPath"]


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
