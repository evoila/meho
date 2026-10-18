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
* #3764: the bounded closure keeps only rows inside the depth, node and
  byte limits, names the limit that stopped it, and rejects a budget
  that is not positive.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import MappingProxyType, SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError

import meho_backplane.topology.query as query_module
from meho_backplane.topology.evidence import (
    FRESHNESS_COPY,
    MAPPED_RELATION_KINDS,
    OBSERVATION_NOTE,
    STALE_SHOWN_COPY,
    classify_freshness,
    relation_class_for,
    relation_class_kind_filter,
    stale_rows_evidence,
)
from meho_backplane.topology.query import ClosureBudget, _cut_closure_rows, bounded_closure
from meho_backplane.topology.resolvers import NodeNotFoundError
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
        # A refresh never changes or removes an operator-stated row, so
        # its age is not a sign that anything changed.
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


def test_observation_note_explains_both_last_seen_sources() -> None:
    """``last_seen`` has two sources: a refresh for auto rows, an operator
    entry for curated rows (a refresh may bump it later). The note must
    not call every value a refresh time."""
    assert "source auto" in OBSERVATION_NOTE
    assert "last refresh that reported the row" in OBSERVATION_NOTE
    assert "source curated" in OBSERVATION_NOTE
    assert "entered or re-stated the row" in OBSERVATION_NOTE
    assert "later refresh that also observed it" in OBSERVATION_NOTE


def test_asserted_copy_keeps_operator_assertion_distinct_from_observation() -> None:
    """A refresh can move a curated row's ``last_seen`` but never changes
    or removes the row; the copy must not say MEHO never refreshes it."""
    asserted = FRESHNESS_COPY["asserted"]
    assert asserted.startswith("An operator stated this.")
    assert "never changes or removes it" in asserted
    assert "later refresh that also observed it" in asserted
    assert "does not refresh" not in asserted


def test_stale_copy_does_not_assume_include_stale_is_true() -> None:
    """The base ``stale`` text is neutral; the reason the row is shown
    depends on the selected stale view."""
    assert "include_stale" not in FRESHNESS_COPY["stale"]
    assert "include_stale is true" in STALE_SHOWN_COPY[True]
    assert "named it" in STALE_SHOWN_COPY[False]
    assert "other stale rows are excluded" in STALE_SHOWN_COPY[False]


def test_stale_rows_evidence_reports_the_named_row_exception() -> None:
    included = stale_rows_evidence(include_stale=True, named_rows="the root you named")
    assert included["stale_rows"] == "included"
    assert "include_stale is true" in included["stale_rows_note"]

    closure = stale_rows_evidence(include_stale=False, named_rows="the root you named")
    assert closure["stale_rows"] == "excluded"
    note = closure["stale_rows_note"]
    assert "include_stale is false" in note
    assert "the root you named, which MEHO always shows, even when stale" in note

    path = stale_rows_evidence(include_stale=False, named_rows="the two endpoints you named")
    assert "the two endpoints you named" in path["stale_rows_note"]

    edges = stale_rows_evidence(include_stale=False, named_rows=None)
    assert edges["stale_rows"] == "excluded"
    assert edges["stale_rows_note"] == (
        "Rows the latest refresh no longer reported are never listed."
    )


# ---------------------------------------------------------------------------
# #3764 — cutting traversal rows at the closure limits (no database)
# ---------------------------------------------------------------------------


class _FakeRow:
    """Stand-in for a traversal result row: only ``_mapping`` is read."""

    def __init__(self, name: str, depth: int, cumulative_bytes: int) -> None:
        self._mapping = {
            "id": uuid4(),
            "kind": "vm",
            "name": name,
            "source": "auto",
            "source_scope": None,
            "properties": {},
            "last_seen": None,
            "depth": depth,
            "via_edge_kind": None if depth == 0 else "runs-on",
            "parent_node_id": None,
            "via_edge_id": None,
            "via_edge_source": None,
            "via_edge_last_seen": None,
            "cumulative_bytes": cumulative_bytes,
        }


def _new_star_rows(leaves: int, *, row_bytes: int = 10, depth: int = 1) -> list[_FakeRow]:
    """New #3764 fixture: a root and *leaves* rows at *depth*, in answer order."""
    rows = [_FakeRow("root", 0, row_bytes)]
    rows += [_FakeRow(f"leaf-{i:04d}", depth, row_bytes * (i + 2)) for i in range(leaves)]
    return rows


def test_cut_closure_rows_keeps_everything_when_no_limit_applies() -> None:
    nodes, limit = _cut_closure_rows(_new_star_rows(3), depth=1, budget=ClosureBudget())
    assert limit is None
    assert [n.name for n in nodes] == ["root", "leaf-0000", "leaf-0001", "leaf-0002"]


def test_cut_closure_rows_stops_the_new_star_at_the_node_limit() -> None:
    rows = _new_star_rows(10)[:5]  # the SQL sends at most max_nodes + 1 rows
    nodes, limit = _cut_closure_rows(rows, depth=1, budget=ClosureBudget(max_nodes=4))
    assert limit == "node_limit"
    assert [n.name for n in nodes] == ["root", "leaf-0000", "leaf-0001", "leaf-0002"]


def test_cut_closure_rows_stops_the_new_star_at_the_byte_limit() -> None:
    nodes, limit = _cut_closure_rows(
        _new_star_rows(10), depth=1, budget=ClosureBudget(max_bytes=35)
    )
    assert limit == "byte_limit"
    assert [n.name for n in nodes] == ["root", "leaf-0000", "leaf-0001"]


def test_cut_closure_rows_keeps_an_oversized_root_and_reports_bytes() -> None:
    nodes, limit = _cut_closure_rows(
        [_FakeRow("root", 0, 100)], depth=1, budget=ClosureBudget(max_bytes=10)
    )
    assert limit == "byte_limit"
    assert [n.name for n in nodes] == ["root"]


def test_cut_closure_rows_reports_hop_limit_for_a_row_past_the_depth() -> None:
    rows = [*_new_star_rows(2), _FakeRow("deeper", 2, 1000)]
    nodes, limit = _cut_closure_rows(rows, depth=1, budget=ClosureBudget(max_bytes=40))
    assert limit == "hop_limit"
    assert [n.name for n in nodes] == ["root", "leaf-0000", "leaf-0001"]


@pytest.mark.parametrize(
    "budget",
    [ClosureBudget(max_nodes=0), ClosureBudget(max_bytes=0), ClosureBudget(time_budget_ms=0)],
)
async def test_bounded_closure_rejects_a_non_positive_budget(budget: ClosureBudget) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        await bounded_closure(None, "x", reverse=True, budget=budget)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# #3764 — the hop-by-hop walk stops asking the database at the limits
# (fake session, no database)
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows: list[_FakeRow]) -> None:
        self._rows = rows

    def fetchall(self) -> list[_FakeRow]:
        return self._rows


class _CancelledError(Exception):
    """What asyncpg raises when ``statement_timeout`` stops a statement."""

    sqlstate = "57014"


class _FakeSession:
    """Answers the lookup, anchor and hop statements from canned rows; records them.

    ``statements`` is every statement in the order the walk sent it;
    ``asked`` is the bind parameters of each hop statement.
    """

    def __init__(
        self,
        hops: dict[int, list[_FakeRow]],
        *,
        cancel_hop: int | None = None,
        cancel_lookup: bool = False,
        root_found: bool = True,
        anchor_bytes: int = 10,
        delay_s: float = 0.0,
    ) -> None:
        self.hops = hops
        self.cancel_hop = cancel_hop
        self.cancel_lookup = cancel_lookup
        self.root_found = root_found
        self.delay_s = delay_s
        self.root = SimpleNamespace(
            id=uuid4(), kind="host", name="root", source="auto", source_scope=None, last_seen=None
        )
        self.anchor = _FakeRow("root", 0, anchor_bytes)
        self.anchor._mapping["id"] = self.root.id
        self.statements: list[str] = []
        self.asked: list[dict[str, Any]] = []

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, sql: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        self.statements.append(str(sql))
        params = params or {}
        if "statement_timeout" in str(sql) or "SET TRANSACTION" in str(sql):
            return _FakeResult([])
        if "anchor_id" in params:
            return _FakeResult([self.anchor])
        if "frontier" not in params:  # the identity-only root lookup
            if self.cancel_lookup:
                raise DBAPIError("lookup", {}, _CancelledError())
            return _FakeResult([self.root] if self.root_found else [])
        # Copy the bind lists: the walk grows ``visited`` in place after the hop.
        self.asked.append({k: list(v) if isinstance(v, list) else v for k, v in params.items()})
        if params["depth"] == self.cancel_hop:
            raise DBAPIError("hop", {}, _CancelledError())
        return _FakeResult(self.hops.get(params["depth"], [])[: params["row_limit"]])


def _hop_rows(hop: int, count: int, *, row_bytes: int = 10) -> list[_FakeRow]:
    return [_FakeRow(f"hop{hop}-{i:04d}", hop, 10 + row_bytes * (i + 1)) for i in range(count)]


async def _walk(
    session: _FakeSession, monkeypatch: pytest.MonkeyPatch, **kwargs: Any
) -> query_module.BoundedClosure:
    monkeypatch.setattr(query_module, "get_sessionmaker", lambda: lambda: session)
    operator = SimpleNamespace(tenant_id=uuid4())
    return await bounded_closure(operator, "root", reverse=True, **kwargs)  # type: ignore[arg-type]


async def test_walk_starts_on_one_snapshot_and_looks_the_root_up_under_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One read-only snapshot for the whole walk; the lookup runs under the timeout."""
    session = _FakeSession({1: _hop_rows(1, 2)})

    answer = await _walk(session, monkeypatch, depth=1)

    assert answer.limit_hit is None
    assert "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY" in session.statements[0]
    assert "statement_timeout" in session.statements[1]
    lookup = session.statements[2]
    assert "graph_node" in lookup
    # The lookup identifies the root; it never selects the properties column.
    assert "graph_node.properties" not in lookup
    assert answer.root is not None
    assert answer.root.id == session.root.id
    assert answer.root.properties == {}


async def test_walk_raises_for_an_untracked_root_before_any_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession({1: _hop_rows(1, 2)}, root_found=False)

    with pytest.raises(NodeNotFoundError):
        await _walk(session, monkeypatch, depth=1)

    assert session.asked == []


async def test_walk_reports_time_budget_without_a_root_when_the_lookup_runs_out_of_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession({1: _hop_rows(1, 2)}, cancel_lookup=True)

    answer = await _walk(session, monkeypatch, depth=1)

    assert answer.limit_hit == "time_budget"
    assert answer.root is None
    assert answer.nodes == ()
    assert session.asked == []


async def test_walk_keeps_an_oversized_root_without_its_properties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A root over the byte budget is still named; no hop is asked for."""
    session = _FakeSession({1: _hop_rows(1, 2)}, anchor_bytes=5000)

    answer = await _walk(session, monkeypatch, depth=1, budget=ClosureBudget(max_bytes=100))

    assert answer.limit_hit == "byte_limit"
    assert answer.root is not None
    assert [n.name for n in answer.nodes] == ["root"]
    assert answer.nodes[0].properties == {}
    assert session.asked == []


async def test_walk_stops_asking_at_the_node_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first hop is asked for the nodes still allowed; the next hop is never asked."""
    session = _FakeSession({1: _hop_rows(1, 50), 2: _hop_rows(2, 50)})

    answer = await _walk(session, monkeypatch, depth=4, budget=ClosureBudget(max_nodes=10))

    assert answer.limit_hit == "node_limit"
    assert len(answer.nodes) == 10
    assert [hop["depth"] for hop in session.asked] == [1]
    assert session.asked[0]["row_limit"] == 10
    assert session.asked[0]["frontier"] == [str(session.root.id)]


async def test_walk_expands_only_kept_nodes_and_probes_one_hop_past_depth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hops = {1: _hop_rows(1, 3), 2: _hop_rows(2, 2), 3: _hop_rows(3, 5)}
    session = _FakeSession(hops)

    answer = await _walk(session, monkeypatch, depth=2)

    assert answer.limit_hit == "hop_limit"
    assert [n.name for n in answer.nodes] == ["root"] + [
        r._mapping["name"] for r in [*hops[1], *hops[2]]
    ]
    root_id = str(session.root.id)
    hop1_ids = [str(r._mapping["id"]) for r in hops[1]]
    hop2_ids = [str(r._mapping["id"]) for r in hops[2]]
    assert [hop["depth"] for hop in session.asked] == [1, 2, 3]
    assert session.asked[1]["frontier"] == hop1_ids
    assert session.asked[1]["visited"] == [root_id, *hop1_ids]
    assert session.asked[2]["frontier"] == hop2_ids
    assert session.asked[2]["visited"] == [root_id, *hop1_ids, *hop2_ids]
    # The probe past ``depth`` only needs to know whether a node exists.
    assert session.asked[2]["row_limit"] == 1
    # Each hop starts from the running byte total of the last row in hand.
    assert [hop["bytes_before"] for hop in session.asked] == [
        10,
        hops[1][-1]._mapping["cumulative_bytes"],
        hops[2][-1]._mapping["cumulative_bytes"],
    ]


async def test_walk_stops_asking_at_the_byte_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession({1: _hop_rows(1, 5, row_bytes=100), 2: _hop_rows(2, 5)})

    answer = await _walk(session, monkeypatch, depth=4, budget=ClosureBudget(max_bytes=250))

    assert answer.limit_hit == "byte_limit"
    assert [n.name for n in answer.nodes] == ["root", "hop1-0000", "hop1-0001"]
    assert [hop["depth"] for hop in session.asked] == [1]


async def test_walk_stops_asking_when_a_hop_runs_out_of_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession({1: _hop_rows(1, 3), 2: _hop_rows(2, 3)}, cancel_hop=2)

    answer = await _walk(session, monkeypatch, depth=4)

    assert answer.limit_hit == "time_budget"
    assert answer.nodes == ()
    assert answer.root is not None
    assert answer.root.name == "root"
    assert [hop["depth"] for hop in session.asked] == [1, 2]


async def test_walk_stops_asking_once_the_deadline_has_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The time budget covers the whole walk, not each statement on its own."""
    session = _FakeSession({1: _hop_rows(1, 3)}, delay_s=0.002)

    answer = await _walk(session, monkeypatch, depth=4, budget=ClosureBudget(time_budget_ms=1))

    assert answer.limit_hit == "time_budget"
    assert session.asked == []
