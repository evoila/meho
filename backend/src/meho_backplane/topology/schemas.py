# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pydantic models for topology query inputs and result shapes.

Task #451 (G9.1-T4). The three query verbs in
:mod:`meho_backplane.topology.query` return these frozen models. The
shapes mirror the immutability discipline the connector
:class:`~meho_backplane.connectors.schemas.NodeHint` family already
established: ``properties`` round-trips as a plain ``dict`` over the
wire but is wrapped in :class:`types.MappingProxyType` after validation
so a frozen :class:`TopologyNode` is deeply immutable end to end. A
caller cannot mutate a node's ``properties`` bag and have that leak
back into a shared result list.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

__all__ = ["TopologyNode", "TopologyPath"]


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
        object.__setattr__(self, "properties", MappingProxyType(dict(self.properties)))
        return self

    @field_serializer("properties")
    def _serialize_properties(self, value: dict[str, Any]) -> dict[str, Any]:
        return dict(value)


class TopologyPath(BaseModel):
    """An ordered shortest path between two nodes.

    ``nodes`` runs from the ``from`` node (``depth == 0``) to the
    ``to`` node (``depth == total_hops``) inclusive. ``total_hops`` is
    the number of edges traversed, i.e. ``len(nodes) - 1``. v0.2 is
    unweighted: every edge costs one hop.
    """

    model_config = ConfigDict(frozen=True)

    nodes: tuple[TopologyNode, ...]
    total_hops: int
