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
* #3760: relation classes map only the reviewed edge kinds, and the
  freshness rules keep stale, old, observed and curated rows apart.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from uuid import uuid4

import pytest
from pydantic import ValidationError

from meho_backplane.topology.evidence import (
    FRESHNESS_COPY,
    MAPPED_RELATION_KINDS,
    OBSERVATION_NOTE,
    classify_freshness,
    relation_class_for,
    relation_class_kind_filter,
)
from meho_backplane.topology.schemas import (
    TopologyEdge,
    TopologyEdgeEndpoint,
    TopologyNode,
    TopologyPath,
)


def _node(name: str, *, depth: int = 0, properties: dict | None = None) -> TopologyNode:
    return TopologyNode(
        id=uuid4(),
        kind="vm",
        name=name,
        source="auto",
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
    # #2538 chain provenance: always serialised, defaulting to null so
    # a consumer can rely on the keys existing on every node.
    assert parsed["parent_node_id"] is None
    assert parsed["via_edge_id"] is None


# ---------------------------------------------------------------------------
# #2538 — chain provenance fields (parent_node_id / via_edge_id)
# ---------------------------------------------------------------------------


def test_node_provenance_fields_default_to_none() -> None:
    """Pre-#2538 constructors (no provenance kwargs) stay valid."""
    node = _node("vm1")
    assert node.parent_node_id is None
    assert node.via_edge_id is None


def test_node_provenance_fields_round_trip() -> None:
    parent = uuid4()
    edge = uuid4()
    node = TopologyNode(
        id=uuid4(),
        kind="vm",
        name="vm1",
        source="auto",
        depth=1,
        via_edge_kind="runs-on",
        parent_node_id=parent,
        via_edge_id=edge,
    )
    assert node.parent_node_id == parent
    assert node.via_edge_id == edge
    parsed = json.loads(node.model_dump_json())
    assert parsed["parent_node_id"] == str(parent)
    assert parsed["via_edge_id"] == str(edge)


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


# ---------------------------------------------------------------------------
# Source scope (#3739 expand step)
# ---------------------------------------------------------------------------

_SCOPE_UUID = "0f8fad5b-d9cb-469f-a165-70867728950e"


def _scoped_node(source_scope: str | None) -> TopologyNode:
    return TopologyNode(
        id=uuid4(),
        kind="namespace",
        name="default",
        source="auto",
        depth=0,
        via_edge_kind=None,
        source_scope=source_scope,
    )


def test_legacy_node_without_scope_reads_unresolved() -> None:
    node = TopologyNode(
        id=uuid4(), kind="vm", name="vm1", source="auto", depth=0, via_edge_kind=None
    )

    assert node.source_scope is None
    assert node.scope_status == "unresolved"
    dumped = json.loads(node.model_dump_json())
    assert dumped["source_scope"] is None
    assert dumped["scope_status"] == "unresolved"


@pytest.mark.parametrize(
    ("scope", "status"),
    [
        ("global", "global"),
        (f"target:{_SCOPE_UUID}", "target"),
        (f"legacy:{_SCOPE_UUID}", "legacy"),
    ],
)
def test_documented_scope_forms_are_accepted(scope: str, status: str) -> None:
    node = _scoped_node(scope)

    assert node.source_scope == scope
    assert node.scope_status == status
    assert node.model_dump()["scope_status"] == status


@pytest.mark.parametrize(
    "scope",
    [
        "",
        "Global",
        f"target:{_SCOPE_UUID.upper()}",
        f"cluster:{_SCOPE_UUID}",
        f"target:{_SCOPE_UUID[:-1]}",
        f"target:{_SCOPE_UUID.replace('-', '')}",
        f"legacy:{_SCOPE_UUID[:-1]}g",
        "legacy:",
    ],
)
def test_malformed_scope_forms_are_rejected(scope: str) -> None:
    with pytest.raises(ValidationError):
        _scoped_node(scope)


# ---------------------------------------------------------------------------
# #3760 — relation class and freshness
# ---------------------------------------------------------------------------


def test_only_the_reviewed_kinds_are_mapped() -> None:
    assert MAPPED_RELATION_KINDS == {
        "belongs-to": "containment",
        "runs-on": "placement",
        "authenticates-via": "dependency",
    }


@pytest.mark.parametrize(
    ("edge_kind", "relation_class"),
    [
        ("belongs-to", "containment"),
        ("runs-on", "placement"),
        ("authenticates-via", "dependency"),
        # Unmapped kinds stay unknown, even ones whose name sounds like
        # a dependency. MEHO never infers a dependency.
        ("depends-on", "unknown"),
        ("mounts", "unknown"),
        ("routes-through", "unknown"),
        ("some-new-kind", "unknown"),
        (None, None),
    ],
)
def test_relation_class_for_edge_kind(edge_kind: str | None, relation_class: str | None) -> None:
    assert relation_class_for(edge_kind) == relation_class


def test_relation_class_kind_filter_binds() -> None:
    assert relation_class_kind_filter(None) == (None, None)
    assert relation_class_kind_filter("containment") == (["belongs-to"], None)
    assert relation_class_kind_filter("placement") == (["runs-on"], None)
    assert relation_class_kind_filter("dependency") == (["authenticates-via"], None)
    # ``unknown`` keeps every kind that is NOT mapped.
    assert relation_class_kind_filter("unknown") == (
        None,
        ["authenticates-via", "belongs-to", "runs-on"],
    )
    with pytest.raises(ValueError, match="relation_class"):
        relation_class_kind_filter("service")


def test_node_and_edge_carry_relation_class() -> None:
    root = _node("ns")
    member = TopologyNode(
        id=uuid4(),
        kind="pod",
        name="pod-1",
        source="auto",
        depth=1,
        via_edge_kind="belongs-to",
    )
    other = TopologyNode(
        id=uuid4(),
        kind="svc",
        name="svc-1",
        source="curated",
        depth=1,
        via_edge_kind="depends-on",
    )
    assert root.via_relation_class is None
    assert member.via_relation_class == "containment"
    assert other.via_relation_class == "unknown"
    assert json.loads(member.model_dump_json())["via_relation_class"] == "containment"

    edge = TopologyEdge(
        id=uuid4(),
        from_endpoint=TopologyEdgeEndpoint(id=uuid4(), kind="vm", name="vm1"),
        to_endpoint=TopologyEdgeEndpoint(
            id=uuid4(),
            kind="host",
            name="h1",
            source_scope="target:0f8fad5b-d9cb-469f-a165-70867728950e",
        ),
        kind="runs-on",
        source="auto",
        last_seen=None,
    )
    dumped = edge.model_dump(mode="json")
    assert dumped["relation_class"] == "placement"
    assert dumped["from_endpoint"]["source_scope"] is None
    assert dumped["to_endpoint"]["source_scope"].startswith("target:")


def test_path_echoes_direction_with_undirected_default() -> None:
    assert TopologyPath(nodes=(_node("a"),), total_hops=0).direction == "undirected"
    path = TopologyPath(nodes=(_node("a"),), total_hops=0, direction="forward")
    assert path.model_dump()["direction"] == "forward"


_NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
_HOUR = 3600


@pytest.mark.parametrize(
    ("source", "last_seen", "expected"),
    [
        # Soft-deleted by the latest refresh.
        ("auto", None, "stale"),
        ("curated", None, "stale"),
        # Readable but seen more than two refresh cycles ago.
        ("auto", _NOW - timedelta(hours=3), "old_observation"),
        # Seen in a recent refresh.
        ("auto", _NOW - timedelta(minutes=30), "observed"),
        ("auto", _NOW - timedelta(hours=2), "observed"),
        # Operator-stated rows are not refreshed, so their age is not a
        # sign that anything changed.
        ("curated", _NOW - timedelta(days=30), "asserted"),
    ],
)
def test_classify_freshness(source: str, last_seen: datetime | None, expected: str) -> None:
    assert (
        classify_freshness(source, last_seen, now=_NOW, refresh_interval_seconds=_HOUR) == expected
    )


def test_freshness_copy_never_claims_vendor_time_or_one_snapshot() -> None:
    assert set(FRESHNESS_COPY) == {"observed", "old_observation", "stale", "asserted"}
    # Each category has its own text.
    assert len(set(FRESHNESS_COPY.values())) == 4
    assert "not the vendor's own timestamp" in OBSERVATION_NOTE
    assert "not one consistent snapshot" in OBSERVATION_NOTE
