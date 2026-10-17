# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pure planning for the topology scope cut-over (#3749).

:func:`build_plan` takes the graph as it is now (:class:`OldNode`,
:class:`OldEdge`) and what every target reported when asked again
(:class:`TargetDiscovery`), and returns a :class:`RepairPlan`. It does no
I/O, so the same inputs always give the same plan, whatever order the
targets were asked in.

Rules for one tenant
--------------------

A *claim* is one target reporting a node. The node's scope comes from
:func:`~meho_backplane.topology.identity_mode.scope_for_discovered_node`
(``target:<id>``, or ``global`` for the target's own node).

For every existing row, keyed by ``(kind, name)`` (unique before the
cut-over):

* claimed in exactly one scope -> ``adopt``: the row keeps its id,
  history and edges and gets that scope;
* claimed in two or more scopes -> ``legacy``: the row was two things at
  once. It is set aside as ``legacy:<its id>``; each claiming target
  gets a new row;
* not claimed, and ``kind='target'`` or ``source='curated'`` ->
  ``keep`` as ``global`` (the registered target itself, or an operator
  seed);
* not claimed, probe-made -> ``legacy`` (``not_rediscovered``).

Every claim without an existing row is ``publish``: a new row.

Discovered edges link the final node ids. An edge whose end the same
target did not report is a problem (``unresolved_edge``). An existing
edge with the same ends and kind is adopted; otherwise the edge is
published. Every other existing edge is accounted for too:

* an edge that touches a ``legacy`` row is ``quarantine``
  (``endpoint_set_aside``): it stays for history but leaves every graph
  answer, because readers skip the legacy row;
* a probe-made edge no target reported again is ``quarantine``
  (``not_rediscovered``): the cut-over writes its history tombstone and
  deletes the row. Such an edge was another target's claim on a row
  that now belongs to one target, so keeping it would join two targets
  that share nothing;
* a curated edge between rows that stay readable is operator data and
  is left as it is.

The plan is *complete* only when every target answered and there is no
problem. The cut-over refuses an incomplete plan.

The plan stores keys, scopes, ids and reasons only. It never stores node
or edge properties: a new row starts with empty properties and the next
refresh fills them in.
"""

from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from meho_backplane.connectors.schemas import TopologyHints
from meho_backplane.db.models import SOURCE_SCOPE_GLOBAL
from meho_backplane.topology.identity_mode import scope_for_discovered_node

__all__ = [
    "PLAN_SCHEMA_VERSION",
    "OldEdge",
    "OldNode",
    "PlanProblem",
    "PlannedEdge",
    "PlannedNode",
    "RepairPlan",
    "TargetDiscovery",
    "TargetReport",
    "build_plan",
    "snapshot_version",
]

#: Layout version of the JSON ``body`` stored in ``topology_scope_manifest``.
PLAN_SCHEMA_VERSION = 1

NodeAction = Literal["adopt", "keep", "publish", "legacy"]
EdgeAction = Literal["adopt", "publish", "quarantine"]

_NodeKey = tuple[str, str, str]  # (scope, kind, name)


class OldNode(BaseModel):
    """One existing ``graph_node`` row, as far as the plan needs it."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    kind: str
    name: str
    source: str
    source_scope: str | None
    live: bool


class OldEdge(BaseModel):
    """One existing ``graph_edge`` row, as far as the plan needs it."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    from_node_id: uuid.UUID
    to_node_id: uuid.UUID
    kind: str
    source: str
    live: bool


class TargetDiscovery(BaseModel):
    """What one target reported. ``hints is None`` means it did not answer."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    tenant_id: uuid.UUID
    target_id: uuid.UUID
    discovered_by: str
    observed_at: datetime
    hints: TopologyHints | None
    failure: str | None = None


class TargetReport(BaseModel):
    """Per-target line of the manifest."""

    model_config = ConfigDict(frozen=True)

    tenant_id: uuid.UUID
    target_id: uuid.UUID
    available: bool
    failure: str | None
    node_count: int
    edge_count: int


class PlannedNode(BaseModel):
    """One node decision. ``node_id`` is the row id after the cut-over."""

    model_config = ConfigDict(frozen=True)

    tenant_id: uuid.UUID
    kind: str
    name: str
    scope: str
    action: NodeAction
    node_id: uuid.UUID
    old_node_id: uuid.UUID | None
    source: str
    target_id: uuid.UUID | None
    discovered_by: str | None
    observed_at: datetime | None
    reason: str


class PlannedEdge(BaseModel):
    """One edge decision. ``edge_id`` is the row id after the cut-over."""

    model_config = ConfigDict(frozen=True)

    tenant_id: uuid.UUID
    kind: str
    from_node_id: uuid.UUID
    to_node_id: uuid.UUID
    action: EdgeAction
    edge_id: uuid.UUID
    old_edge_id: uuid.UUID | None
    source: str
    target_id: uuid.UUID | None
    discovered_by: str | None
    observed_at: datetime | None
    reason: str


class PlanProblem(BaseModel):
    """Something that blocks the cut-over until a new staging run."""

    model_config = ConfigDict(frozen=True)

    code: Literal["target_unavailable", "unresolved_edge", "global_name_claimed_twice"]
    tenant_id: uuid.UUID
    target_id: uuid.UUID | None
    detail: str


class RepairPlan(BaseModel):
    """The manifest body: targets, node and edge decisions, problems."""

    model_config = ConfigDict(frozen=True)

    schema_version: int = PLAN_SCHEMA_VERSION
    targets: tuple[TargetReport, ...]
    nodes: tuple[PlannedNode, ...]
    edges: tuple[PlannedEdge, ...]
    problems: tuple[PlanProblem, ...]

    @property
    def complete(self) -> bool:
        """True when every target answered and nothing is unresolved."""
        return not self.problems and all(t.available for t in self.targets)


def snapshot_version(nodes: Iterable[OldNode], edges: Iterable[OldEdge]) -> str:
    """Fingerprint the parts of the graph the plan depends on.

    Covers row ids, keys, ``source``, ``source_scope`` and whether a row
    is live. Leaves out properties, timestamps and ``target_id``: a
    refresh rewrites those on every run (``target_id`` flips between the
    targets that share a name), and the plan does not use them. A new,
    removed, renamed, promoted or soft-deleted row changes the value.
    """
    digest = hashlib.sha256()
    for n in sorted(nodes, key=lambda r: r.id):
        digest.update(
            f"n|{n.id}|{n.tenant_id}|{n.kind}|{n.name}|{n.source}|"
            f"{n.source_scope}|{int(n.live)}\n".encode()
        )
    for e in sorted(edges, key=lambda r: r.id):
        digest.update(
            f"e|{e.id}|{e.tenant_id}|{e.from_node_id}|{e.to_node_id}|{e.kind}|"
            f"{e.source}|{int(e.live)}\n".encode()
        )
    return digest.hexdigest()


class _TenantStage:
    """Claims collected for one tenant: who reported which scoped key."""

    def __init__(self, discoveries: list[TargetDiscovery]) -> None:
        self.discoveries = discoveries
        self.claims: dict[_NodeKey, set[uuid.UUID]] = defaultdict(set)
        self.by_target: dict[uuid.UUID, TargetDiscovery] = {}
        self.targets: list[TargetReport] = []
        self.problems: list[PlanProblem] = []
        for d in sorted(discoveries, key=lambda x: x.target_id):
            self._collect(d)
        self.conflicts = {
            k for k, owners in self.claims.items() if len(owners) > 1
        }  # only possible for global keys: target scopes are per target
        for scope, kind, name in sorted(self.conflicts):
            self.problems.append(
                PlanProblem(
                    code="global_name_claimed_twice",
                    tenant_id=discoveries[0].tenant_id,
                    target_id=None,
                    detail=f"{scope} {kind}/{name}",
                )
            )

    def _collect(self, d: TargetDiscovery) -> None:
        if d.hints is None:
            self.targets.append(
                TargetReport(
                    tenant_id=d.tenant_id,
                    target_id=d.target_id,
                    available=False,
                    failure=d.failure or "no_answer",
                    node_count=0,
                    edge_count=0,
                )
            )
            self.problems.append(
                PlanProblem(
                    code="target_unavailable",
                    tenant_id=d.tenant_id,
                    target_id=d.target_id,
                    detail=d.failure or "no_answer",
                )
            )
            return
        self.by_target[d.target_id] = d
        for n in d.hints.nodes:
            self.claims[(scope_for_discovered_node(d.target_id, n.kind), n.kind, n.name)].add(
                d.target_id
            )
        self.targets.append(
            TargetReport(
                tenant_id=d.tenant_id,
                target_id=d.target_id,
                available=True,
                failure=None,
                node_count=len(d.hints.nodes),
                edge_count=len(d.hints.edges),
            )
        )

    def scopes_for(self, kind: str, name: str) -> list[str]:
        """Every scope some target claims ``(kind, name)`` in, sorted."""
        return sorted({s for (s, k, n) in self.claims if k == kind and n == name})

    def claimant(self, key: _NodeKey) -> TargetDiscovery:
        """The single target behind a non-conflicting key."""
        (target_id,) = self.claims[key]
        return self.by_target[target_id]


def _old_node_decision(
    row: OldNode, stage: _TenantStage
) -> tuple[NodeAction, str, str, _NodeKey | None]:
    """Return ``(action, scope, reason, claimed key)`` for one existing row."""
    scopes = stage.scopes_for(row.kind, row.name)
    if len(scopes) == 1 and (scopes[0], row.kind, row.name) not in stage.conflicts:
        key = (scopes[0], row.kind, row.name)
        return "adopt", scopes[0], "rediscovered", key
    if len(scopes) >= 1:
        return "legacy", f"legacy:{row.id}", "claimed_by_several_targets", None
    if row.kind == "target":
        return "keep", SOURCE_SCOPE_GLOBAL, "target_node", None
    if row.source == "curated":
        return "keep", SOURCE_SCOPE_GLOBAL, "curated", None
    return "legacy", f"legacy:{row.id}", "not_rediscovered", None


def _plan_nodes(
    tenant_id: uuid.UUID,
    old_nodes: list[OldNode],
    stage: _TenantStage,
    new_id: Callable[[], uuid.UUID],
) -> tuple[list[PlannedNode], dict[_NodeKey, uuid.UUID]]:
    planned: list[PlannedNode] = []
    final_id: dict[_NodeKey, uuid.UUID] = {}
    for row in sorted(old_nodes, key=lambda r: (r.kind, r.name)):
        action, scope, reason, key = _old_node_decision(row, stage)
        claimant = stage.claimant(key) if key is not None else None
        if key is not None:
            final_id[key] = row.id
        planned.append(
            PlannedNode(
                tenant_id=tenant_id,
                kind=row.kind,
                name=row.name,
                scope=scope,
                action=action,
                node_id=row.id,
                old_node_id=row.id,
                source=row.source,
                target_id=claimant.target_id if claimant else None,
                discovered_by=claimant.discovered_by if claimant else None,
                observed_at=claimant.observed_at if claimant else None,
                reason=reason,
            )
        )
    for key in sorted(stage.claims):
        if key in final_id or key in stage.conflicts:
            continue
        claimant = stage.claimant(key)
        final_id[key] = new_id()
        planned.append(
            PlannedNode(
                tenant_id=tenant_id,
                kind=key[1],
                name=key[2],
                scope=key[0],
                action="publish",
                node_id=final_id[key],
                old_node_id=None,
                source="auto",
                target_id=claimant.target_id,
                discovered_by=claimant.discovered_by,
                observed_at=claimant.observed_at,
                reason="newly_scoped",
            )
        )
    return planned, final_id


def _plan_discovered_edges(
    tenant_id: uuid.UUID,
    stage: _TenantStage,
    final_id: dict[_NodeKey, uuid.UUID],
    old_by_ends: dict[tuple[uuid.UUID, uuid.UUID, str], OldEdge],
    new_id: Callable[[], uuid.UUID],
) -> list[PlannedEdge]:
    planned: list[PlannedEdge] = []
    seen: set[tuple[uuid.UUID, uuid.UUID, str]] = set()
    for target_id in sorted(stage.by_target):
        d = stage.by_target[target_id]
        assert d.hints is not None  # by_target only holds answering targets
        own = {
            (scope_for_discovered_node(target_id, n.kind), n.kind, n.name) for n in d.hints.nodes
        }
        for e in sorted(
            d.hints.edges, key=lambda h: (h.from_kind, h.from_name, h.to_kind, h.to_name, h.kind)
        ):
            fk = (scope_for_discovered_node(target_id, e.from_kind), e.from_kind, e.from_name)
            tk = (scope_for_discovered_node(target_id, e.to_kind), e.to_kind, e.to_name)
            if fk not in own or tk not in own:
                stage.problems.append(
                    PlanProblem(
                        code="unresolved_edge",
                        tenant_id=tenant_id,
                        target_id=target_id,
                        detail=f"{e.from_kind}/{e.from_name} -{e.kind}-> {e.to_kind}/{e.to_name}",
                    )
                )
                continue
            if fk not in final_id or tk not in final_id:
                continue  # an end is a global conflict, already a problem
            ends = (final_id[fk], final_id[tk], e.kind)
            if ends in seen:
                continue
            seen.add(ends)
            old = old_by_ends.get(ends)
            planned.append(
                PlannedEdge(
                    tenant_id=tenant_id,
                    kind=e.kind,
                    from_node_id=ends[0],
                    to_node_id=ends[1],
                    action="adopt" if old else "publish",
                    edge_id=old.id if old else new_id(),
                    old_edge_id=old.id if old else None,
                    source=old.source if old else "auto",
                    target_id=target_id,
                    discovered_by=d.discovered_by,
                    observed_at=d.observed_at,
                    reason="rediscovered" if old else "newly_scoped",
                )
            )
    return planned


def _quarantine_reason(edge: OldEdge, scope_of: dict[uuid.UUID, str]) -> str | None:
    """Why an existing edge no target reported again must leave the graph."""
    ends = (scope_of.get(edge.from_node_id, ""), scope_of.get(edge.to_node_id, ""))
    if any(scope.startswith("legacy:") for scope in ends):
        return "endpoint_set_aside"
    if edge.source != "curated":
        return "not_rediscovered"
    return None


def _plan_leftover_edges(
    tenant_id: uuid.UUID,
    old_edges: list[OldEdge],
    nodes: list[PlannedNode],
    adopted: set[uuid.UUID],
) -> list[PlannedEdge]:
    """Quarantine every old edge no discovery adopted and the new graph cannot keep."""
    scope_of = {n.node_id: n.scope for n in nodes if n.old_node_id is not None}
    planned: list[PlannedEdge] = []
    for e in sorted(old_edges, key=lambda r: r.id):
        if e.id in adopted:
            continue
        reason = _quarantine_reason(e, scope_of)
        if reason is None:
            continue
        planned.append(
            PlannedEdge(
                tenant_id=tenant_id,
                kind=e.kind,
                from_node_id=e.from_node_id,
                to_node_id=e.to_node_id,
                action="quarantine",
                edge_id=e.id,
                old_edge_id=e.id,
                source=e.source,
                target_id=None,
                discovered_by=None,
                observed_at=None,
                reason=reason,
            )
        )
    return planned


def build_plan(
    old_nodes: Iterable[OldNode],
    old_edges: Iterable[OldEdge],
    discoveries: Iterable[TargetDiscovery],
    *,
    new_id: Callable[[], uuid.UUID] = uuid.uuid4,
) -> RepairPlan:
    """Plan the cut-over for every tenant. Pure; see the module docstring."""
    nodes_by_tenant: dict[uuid.UUID, list[OldNode]] = defaultdict(list)
    edges_by_tenant: dict[uuid.UUID, list[OldEdge]] = defaultdict(list)
    disc_by_tenant: dict[uuid.UUID, list[TargetDiscovery]] = defaultdict(list)
    for n in old_nodes:
        nodes_by_tenant[n.tenant_id].append(n)
    for e in old_edges:
        edges_by_tenant[e.tenant_id].append(e)
    for d in discoveries:
        disc_by_tenant[d.tenant_id].append(d)

    targets: list[TargetReport] = []
    nodes: list[PlannedNode] = []
    edges: list[PlannedEdge] = []
    problems: list[PlanProblem] = []
    for tenant_id in sorted(set(nodes_by_tenant) | set(disc_by_tenant)):
        stage = _TenantStage(disc_by_tenant.get(tenant_id, []))
        tenant_nodes, final_id = _plan_nodes(
            tenant_id, nodes_by_tenant.get(tenant_id, []), stage, new_id
        )
        old_by_ends = {
            (e.from_node_id, e.to_node_id, e.kind): e for e in edges_by_tenant[tenant_id]
        }
        discovered = _plan_discovered_edges(tenant_id, stage, final_id, old_by_ends, new_id)
        adopted = {e.old_edge_id for e in discovered if e.old_edge_id is not None}
        edges.extend(discovered)
        edges.extend(
            _plan_leftover_edges(tenant_id, edges_by_tenant[tenant_id], tenant_nodes, adopted)
        )
        nodes.extend(tenant_nodes)
        targets.extend(stage.targets)
        problems.extend(stage.problems)
    return RepairPlan(
        targets=tuple(targets),
        nodes=tuple(nodes),
        edges=tuple(edges),
        problems=tuple(problems),
    )
