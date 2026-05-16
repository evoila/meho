# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the topology query result models.

Task #451 (G9.1-T4). These exercise the pure Pydantic v2 contracts of
:class:`~meho_backplane.topology.schemas.TopologyNode` and
:class:`~meho_backplane.topology.schemas.TopologyPath` with no database
— so they run on every sandbox, not only the Docker-gated integration
runners that :mod:`tests.integration.test_topology_query` needs.

Coverage matrix:

* ``TopologyNode.properties`` is **deeply** immutable: top-level,
  nested-dict, and nested-list mutation all fail; ``model_dump`` /
  ``model_dump_json`` round-trip back to a plain mutable ``dict`` /
  ``list``.
* ``TopologyPath`` rejects structurally invalid instances (empty
  ``nodes``; ``total_hops`` disagreeing with ``len(nodes) - 1``) and
  accepts a well-formed one.
"""

from __future__ import annotations

import json
from types import MappingProxyType
from uuid import uuid4

import pytest
from pydantic import ValidationError

from meho_backplane.topology.schemas import TopologyNode, TopologyPath


def _node(name: str, *, depth: int = 0, properties: dict | None = None) -> TopologyNode:
    return TopologyNode(
        id=uuid4(),
        kind="vm",
        name=name,
        properties=properties if properties is not None else {},
        depth=depth,
        via_edge_kind=None,
    )


# ---------------------------------------------------------------------------
# M1 — TopologyNode.properties deep immutability
# ---------------------------------------------------------------------------


def test_node_properties_top_level_is_read_only() -> None:
    node = _node("vm1", properties={"a": 1})
    assert isinstance(node.properties, MappingProxyType)
    with pytest.raises(TypeError):
        node.properties["b"] = 2  # type: ignore[index]


def test_node_properties_nested_dict_is_frozen() -> None:
    node = _node("vm1", properties={"outer": {"inner": 1}})
    assert isinstance(node.properties["outer"], MappingProxyType)
    with pytest.raises(TypeError):
        node.properties["outer"]["inner"] = 99  # type: ignore[index]


def test_node_properties_nested_list_is_frozen() -> None:
    node = _node("vm1", properties={"items": [1, 2, {"x": 3}]})
    # Lists are frozen to tuples; the nested dict inside is a proxy too.
    assert isinstance(node.properties["items"], tuple)
    assert isinstance(node.properties["items"][2], MappingProxyType)
    with pytest.raises(AttributeError):
        node.properties["items"].append(4)  # type: ignore[attr-defined]


def test_node_properties_serialises_back_to_plain_mutable_structures() -> None:
    node = _node("vm1", properties={"outer": {"inner": 1}, "items": [1, {"x": 2}]})

    dumped = node.model_dump()
    props = dumped["properties"]
    assert isinstance(props, dict)
    assert isinstance(props["outer"], dict)
    assert isinstance(props["items"], list)
    assert isinstance(props["items"][1], dict)
    # Round-tripped structure is mutable again (no proxy/tuple leaked).
    props["outer"]["inner"] = 42
    props["items"].append(99)

    # JSON serialisation produces the same plain shape.
    parsed = json.loads(node.model_dump_json())
    assert parsed["properties"] == {"outer": {"inner": 1}, "items": [1, {"x": 2}]}
    assert parsed["via_edge_kind"] is None


# ---------------------------------------------------------------------------
# m1 — TopologyPath structural invariants
# ---------------------------------------------------------------------------


def test_path_rejects_empty_nodes() -> None:
    with pytest.raises(ValidationError):
        TopologyPath(nodes=(), total_hops=0)


def test_path_rejects_negative_total_hops() -> None:
    with pytest.raises(ValidationError):
        TopologyPath(nodes=(_node("a"),), total_hops=-1)


def test_path_rejects_total_hops_not_matching_node_count() -> None:
    with pytest.raises(ValidationError):
        TopologyPath(nodes=(_node("a"), _node("b", depth=1)), total_hops=5)


def test_path_accepts_well_formed_instance() -> None:
    nodes = (_node("a"), _node("b", depth=1), _node("c", depth=2))
    path = TopologyPath(nodes=nodes, total_hops=2)
    assert path.total_hops == len(path.nodes) - 1


def test_single_node_path_is_valid() -> None:
    path = TopologyPath(nodes=(_node("solo"),), total_hops=0)
    assert path.total_hops == 0
    assert len(path.nodes) == 1
