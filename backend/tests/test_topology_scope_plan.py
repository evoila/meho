# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the scope cut-over plan (#3749).

:func:`~meho_backplane.topology.scope_repair.plan.build_plan` is pure, so
these tests need no database. They pin the staging rules:

* two targets that both report namespace ``default`` stage two distinct
  ``target:<uuid>`` identities;
* asking the targets in the opposite order gives the same plan;
* an old row two targets claim is set aside as ``legacy:<id>``, curated
  rows included, and its edges are quarantined;
* one claim adopts the old row (same id); unclaimed curated rows stay
  ``global``; unclaimed probe rows become ``legacy``;
* a probe edge no target reported again is quarantined, so an old edge
  from another target cannot join two targets through an adopted row;
  a curated edge between readable rows is left alone;
* an unavailable target or an edge to an unreported node makes the plan
  incomplete;
* the snapshot ignores refresh noise (``target_id``, properties) but not
  structure; the stored plan carries no properties.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from meho_backplane.connectors.schemas import EdgeHint, NodeHint, TopologyHints
from meho_backplane.topology.scope_repair.plan import (
    OldEdge,
    OldNode,
    RepairPlan,
    TargetDiscovery,
    build_plan,
    snapshot_version,
)

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
TARGET_A = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
TARGET_B = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def _cluster(target_id: uuid.UUID, cluster: str, *namespaces: str) -> TargetDiscovery:
    """A Kubernetes-shaped answer: the target node plus namespaces that belong to it."""
    nodes = [NodeHint(kind="target", name=cluster, properties={"version": "1.30"})]
    edges = []
    for ns in namespaces:
        nodes.append(NodeHint(kind="namespace", name=ns, properties={"status": "Active"}))
        edges.append(
            EdgeHint(
                from_kind="namespace",
                from_name=ns,
                to_kind="target",
                to_name=cluster,
                kind="belongs-to",
            )
        )
    return TargetDiscovery(
        tenant_id=TENANT,
        target_id=target_id,
        discovered_by="kubernetes",
        observed_at=NOW,
        hints=TopologyHints(discovered_at=NOW, nodes=tuple(nodes), edges=tuple(edges)),
    )


def _old(kind: str, name: str, *, source: str = "auto", scope: str | None = None) -> OldNode:
    return OldNode(
        id=uuid.uuid4(),
        tenant_id=TENANT,
        kind=kind,
        name=name,
        source=source,
        source_scope=scope,
        live=True,
    )


def _old_edge(a: OldNode, b: OldNode, *, source: str = "auto") -> OldEdge:
    return OldEdge(
        id=uuid.uuid4(),
        tenant_id=TENANT,
        from_node_id=a.id,
        to_node_id=b.id,
        kind="belongs-to",
        source=source,
        live=True,
    )


def _shape(plan: RepairPlan) -> tuple[set[tuple[str, str, str, str]], set[tuple[object, ...]]]:
    """Order- and id-free view: node (scope, kind, name, action) and edge ends by key."""
    key = {n.node_id: (n.scope, n.kind, n.name) for n in plan.nodes}
    nodes = {(n.scope, n.kind, n.name, n.action) for n in plan.nodes}
    edges = {(key[e.from_node_id], key[e.to_node_id], e.kind, e.action) for e in plan.edges}
    return nodes, edges


def test_same_namespace_from_two_targets_stages_two_scoped_identities() -> None:
    plan = build_plan(
        [], [], [_cluster(TARGET_A, "a", "default"), _cluster(TARGET_B, "b", "default")]
    )

    assert plan.complete
    defaults = {n.scope for n in plan.nodes if n.name == "default"}
    assert defaults == {f"target:{TARGET_A}", f"target:{TARGET_B}"}
    assert {n.scope for n in plan.nodes if n.kind == "target"} == {"global"}
    assert all(n.action == "publish" for n in plan.nodes)
    # Each default links only to its own cluster: no shared node joins them.
    by_id = {n.node_id: n for n in plan.nodes}
    links = {(by_id[e.from_node_id].scope, by_id[e.to_node_id].name) for e in plan.edges}
    assert links == {(f"target:{TARGET_A}", "a"), (f"target:{TARGET_B}", "b")}


def test_reversed_discovery_order_gives_the_same_plan() -> None:
    merged = _old("namespace", "default")
    cluster_a = _old("target", "a", scope="global")
    old_nodes = [merged, cluster_a, _old("namespace", "only-a")]
    old_edges = [_old_edge(merged, cluster_a)]
    a = _cluster(TARGET_A, "a", "default", "only-a")
    b = _cluster(TARGET_B, "b", "default", "kube-system")

    forward = build_plan(old_nodes, old_edges, [a, b])
    backward = build_plan(list(reversed(old_nodes)), old_edges, [b, a])

    assert _shape(forward) == _shape(backward)
    assert forward.complete and backward.complete


def test_name_claimed_by_two_targets_sets_the_old_row_aside() -> None:
    merged = _old("namespace", "default")
    curated = _old("namespace", "shared", source="curated")
    cluster_a = _old("target", "a", scope="global")
    seed = _old("vault-role", "ops", source="curated")
    old_edges = [_old_edge(merged, cluster_a), _old_edge(seed, curated, source="curated")]

    plan = build_plan(
        [merged, curated, cluster_a, seed],
        old_edges,
        [
            _cluster(TARGET_A, "a", "default", "shared"),
            _cluster(TARGET_B, "b", "default", "shared"),
        ],
    )

    by_old = {n.old_node_id: n for n in plan.nodes if n.old_node_id}
    assert by_old[merged.id].action == "legacy"
    assert by_old[merged.id].scope == f"legacy:{merged.id}"
    assert by_old[merged.id].reason == "claimed_by_several_targets"
    assert by_old[curated.id].action == "legacy", "an ambiguous curated row is quarantined"
    assert by_old[cluster_a.id].action == "adopt"
    assert by_old[seed.id].action == "keep"
    published = {(n.scope, n.name) for n in plan.nodes if n.action == "publish"}
    assert (f"target:{TARGET_A}", "default") in published
    assert (f"target:{TARGET_B}", "default") in published
    quarantined = {e.edge_id: e for e in plan.edges if e.action == "quarantine"}
    assert set(quarantined) == {e.id for e in old_edges}
    assert {e.source for e in quarantined.values()} == {"auto", "curated"}


def test_single_claim_adopts_and_unclaimed_rows_get_global_or_legacy() -> None:
    namespace = _old("namespace", "only-a")
    cluster_a = _old("target", "a")
    stale_target = _old("target", "retired")
    curated = _old("vm", "jump-host", source="curated")
    orphan = _old("namespace", "gone")
    old_edge = _old_edge(namespace, cluster_a)

    plan = build_plan(
        [namespace, cluster_a, stale_target, curated, orphan],
        [old_edge],
        [_cluster(TARGET_A, "a", "only-a")],
    )

    by_old = {n.old_node_id: n for n in plan.nodes}
    assert (by_old[namespace.id].action, by_old[namespace.id].scope) == (
        "adopt",
        f"target:{TARGET_A}",
    )
    assert by_old[namespace.id].node_id == namespace.id, "the row keeps its id"
    assert by_old[namespace.id].target_id == TARGET_A
    assert (by_old[cluster_a.id].action, by_old[cluster_a.id].scope) == ("adopt", "global")
    assert (by_old[stale_target.id].action, by_old[stale_target.id].scope) == ("keep", "global")
    assert (by_old[curated.id].action, by_old[curated.id].scope) == ("keep", "global")
    assert by_old[orphan.id].action == "legacy"
    assert by_old[orphan.id].reason == "not_rediscovered"
    (edge,) = plan.edges
    assert (edge.action, edge.edge_id) == ("adopt", old_edge.id)


def _connected(plan: RepairPlan, old_edges: list[OldEdge], a: uuid.UUID, b: uuid.UUID) -> bool:
    """Walk what stays readable: planned live edges plus old edges the plan does not mention."""
    decided = {e.old_edge_id for e in plan.edges if e.old_edge_id is not None}
    live = [(e.from_node_id, e.to_node_id) for e in plan.edges if e.action != "quarantine"]
    live += [(e.from_node_id, e.to_node_id) for e in old_edges if e.id not in decided]
    seen, frontier = {a}, [a]
    while frontier:
        node = frontier.pop()
        for x, y in live:
            for nxt in (y if x == node else None, x if y == node else None):
                if nxt is not None and nxt not in seen:
                    seen.add(nxt)
                    frontier.append(nxt)
    return b in seen


def test_old_edge_from_a_target_that_stopped_reporting_the_row_is_quarantined() -> None:
    merged = _old("namespace", "default")
    cluster_a = _old("target", "a", scope="global")
    cluster_b = _old("target", "b", scope="global")
    to_a = _old_edge(merged, cluster_a)
    to_b = _old_edge(merged, cluster_b)

    plan = build_plan(
        [merged, cluster_a, cluster_b],
        [to_a, to_b],
        [_cluster(TARGET_A, "a", "default"), _cluster(TARGET_B, "b")],
    )

    assert plan.complete
    by_old = {n.old_node_id: n for n in plan.nodes}
    assert (by_old[merged.id].action, by_old[merged.id].scope) == ("adopt", f"target:{TARGET_A}")
    decisions = {e.old_edge_id: (e.action, e.reason) for e in plan.edges}
    assert decisions[to_a.id] == ("adopt", "rediscovered")
    assert decisions[to_b.id] == ("quarantine", "not_rediscovered")
    assert not _connected(plan, [to_a, to_b], cluster_a.id, cluster_b.id)


def test_curated_edge_between_readable_rows_is_left_alone() -> None:
    ns_a = _old("namespace", "only-a")
    ns_b = _old("namespace", "only-b")
    seed = _old("vm", "jump-host", source="curated")
    operator_links = [
        _old_edge(ns_a, seed, source="curated"),
        _old_edge(ns_a, ns_b, source="curated"),
    ]
    stale_probe = _old_edge(ns_a, ns_b)

    plan = build_plan(
        [ns_a, ns_b, seed],
        [*operator_links, stale_probe],
        [_cluster(TARGET_A, "a", "only-a"), _cluster(TARGET_B, "b", "only-b")],
    )

    decisions = {e.old_edge_id: (e.action, e.reason) for e in plan.edges}
    assert all(e.id not in decisions for e in operator_links)
    assert decisions[stale_probe.id] == ("quarantine", "not_rediscovered")


def test_unavailable_target_makes_the_plan_incomplete() -> None:
    down = TargetDiscovery(
        tenant_id=TENANT,
        target_id=TARGET_B,
        discovered_by="kubernetes",
        observed_at=NOW,
        hints=None,
        failure="TimeoutError",
    )
    plan = build_plan([], [], [_cluster(TARGET_A, "a", "default"), down])

    assert not plan.complete
    assert [p.code for p in plan.problems] == ["target_unavailable"]
    report = {t.target_id: t for t in plan.targets}
    assert report[TARGET_B].available is False
    assert report[TARGET_B].failure == "TimeoutError"
    assert all(n.target_id != TARGET_B for n in plan.nodes), "no node is invented for it"


def test_edge_to_an_unreported_node_is_unresolved() -> None:
    answer = _cluster(TARGET_A, "a")
    assert answer.hints is not None
    dangling = TargetDiscovery(
        **{
            **answer.model_dump(exclude={"hints"}),
            "hints": TopologyHints(
                discovered_at=NOW,
                nodes=answer.hints.nodes,
                edges=(
                    EdgeHint(
                        from_kind="namespace",
                        from_name="ghost",
                        to_kind="target",
                        to_name="a",
                        kind="belongs-to",
                    ),
                ),
            ),
        }
    )
    plan = build_plan([], [], [dangling])

    assert not plan.complete
    assert [p.code for p in plan.problems] == ["unresolved_edge"]
    assert plan.edges == ()


def test_snapshot_ignores_refresh_noise_but_not_structure() -> None:
    row = _old("namespace", "default")
    edge = _old_edge(row, row)
    base = snapshot_version([row], [edge])

    assert snapshot_version([row], [edge]) == base
    assert snapshot_version([row.model_copy(update={"live": False})], [edge]) != base
    assert snapshot_version([row, _old("namespace", "new")], [edge]) != base
    assert snapshot_version([row], []) != base
    assert snapshot_version([row.model_copy(update={"source": "curated"})], [edge]) != base


def test_stored_plan_carries_no_properties() -> None:
    plan = build_plan([], [], [_cluster(TARGET_A, "a", "default")])
    body = plan.model_dump(mode="json")

    assert "properties" not in json.dumps(body)
    assert RepairPlan.model_validate(body) == plan
