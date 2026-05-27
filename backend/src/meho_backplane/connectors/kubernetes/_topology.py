# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Topology-populator helpers for :class:`KubernetesConnector`.

G0.14-T12 (#1201) lands the first
:meth:`~meho_backplane.connectors.base.Connector.discover_topology`
override against a shipped connector, closing the v0.6.0 release-body
amendment promise (`claude-rdc-hetzner-dc#697` signal 13).

The populator scope at v0.7 is deliberately minimal:

* one ``target`` :class:`NodeHint` per cluster, properties carrying the
  cluster's Kubernetes server version (same ``VersionApi.get_code``
  payload :meth:`KubernetesConnector.fingerprint` and
  :meth:`KubernetesConnector.about` already issue);
* one ``namespace`` :class:`NodeHint` per namespace, properties built
  from the existing
  :func:`~meho_backplane.connectors.kubernetes.ops_core.namespace_row`
  projection (``status`` / ``age_seconds`` / ``labels``);
* one ``node`` :class:`NodeHint` per cluster node, properties built
  from the existing
  :func:`~meho_backplane.connectors.kubernetes.ops_core.node_row`
  projection (``roles`` / ``version`` (kubelet) / ``kernel``);
* one ``belongs-to`` :class:`EdgeHint` from each namespace to the
  target node;
* one ``belongs-to`` :class:`EdgeHint` from each cluster node to the
  target node.

Pods / services / ingresses / deployments / volumes are explicitly out
of scope for v0.7 — each would multiply the per-refresh API-call cost
(a 100-namespace cluster would mean 100 list calls per refresh tick)
and the v0.7.x deploy hasn't surfaced refresh-cost data yet. Sibling
Tasks under #1139 (or a future G9.4) will land them when the cost
picture justifies it.

The helpers live in this dedicated module (rather than inline on
``connector.py``) for the same reason
:mod:`~meho_backplane.connectors.kubernetes.ops_core` separates its
row-shape helpers from the connector: the tests can pin the wire
shape against synthetic :class:`V1Namespace` / :class:`V1Node` model
instances without booting an event loop.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.kubernetes.ops_core import namespace_row, node_row
from meho_backplane.connectors.schemas import EdgeHint, NodeHint, TopologyHints

if TYPE_CHECKING:
    from kubernetes_asyncio.client.models import V1Namespace, V1Node, VersionInfo

__all__ = [
    "build_target_node_hint",
    "build_topology_hints",
    "namespace_node_hint",
    "namespace_to_target_edge",
    "node_node_hint",
    "node_to_target_edge",
]


def build_target_node_hint(
    target_name: str,
    version: VersionInfo | None,
) -> NodeHint:
    """Build the cluster's ``target`` :class:`NodeHint`.

    ``cluster`` is not in the v0.2 :data:`NodeKind` enum (the enum is
    closed per ``connectors/schemas.py``); represent the cluster as a
    ``target``-kinded node so the refresh service can wire the
    namespace and node ``belongs-to`` edges back to it.

    Properties carry the cluster's Kubernetes server version data —
    the same ``VersionApi.get_code()`` payload :meth:`fingerprint` and
    :meth:`about` already pull, so the populator pays no extra
    round-trip to expose it. ``version`` may be ``None`` (testing /
    network failure tolerance); in that case ``properties`` is an
    empty dict.
    """
    properties: dict[str, Any] = {}
    if version is not None:
        # Mirror the ``about`` op's flat-dict projection so the row's
        # shape is operator-recognisable across both surfaces.
        properties = {
            "git_version": version.git_version,
            "major": version.major,
            "minor": version.minor,
            "platform": version.platform,
        }
    return NodeHint(kind="target", name=target_name, properties=properties)


def namespace_node_hint(ns: V1Namespace, *, now: datetime | None = None) -> NodeHint:
    """Project a :class:`V1Namespace` into a ``namespace`` :class:`NodeHint`.

    Re-uses :func:`namespace_row` so the populator and the
    ``k8s.namespace.list`` op share their wire shape — operators
    inspecting the graph row see the same ``status`` / ``age_seconds``
    / ``labels`` fields they see in the inventory listing.

    The row's ``name`` field is dropped from ``properties`` (it
    becomes :attr:`NodeHint.name`); the rest survives verbatim. A
    namespace whose ``metadata.name`` is ``None`` (a corrupt fixture,
    in practice never seen in the wild — the API server requires it)
    is **not** filtered here; the caller is responsible for skipping
    it before the row would land in the snapshot.
    """
    row = namespace_row(ns, now=now)
    name = row["name"] or ""
    properties = {k: v for k, v in row.items() if k != "name"}
    return NodeHint(kind="namespace", name=name, properties=properties)


def node_node_hint(node: V1Node, *, now: datetime | None = None) -> NodeHint:
    """Project a :class:`V1Node` into a ``node`` :class:`NodeHint`.

    Re-uses :func:`node_row` so the populator and the ``k8s.node.list``
    op share their wire shape. ``roles`` / ``version`` (kubelet) /
    ``kernel`` are the load-bearing fields per the issue body's
    desired-state spec; the rest of the projection rides along.
    """
    row = node_row(node, now=now)
    name = row["name"] or ""
    properties = {k: v for k, v in row.items() if k != "name"}
    return NodeHint(kind="node", name=name, properties=properties)


def namespace_to_target_edge(namespace_name: str, target_name: str) -> EdgeHint:
    """Build the ``namespace --belongs-to--> target`` :class:`EdgeHint`."""
    return EdgeHint(
        from_kind="namespace",
        from_name=namespace_name,
        to_kind="target",
        to_name=target_name,
        kind="belongs-to",
    )


def node_to_target_edge(node_name: str, target_name: str) -> EdgeHint:
    """Build the ``node --belongs-to--> target`` :class:`EdgeHint`."""
    return EdgeHint(
        from_kind="node",
        from_name=node_name,
        to_kind="target",
        to_name=target_name,
        kind="belongs-to",
    )


def build_topology_hints(
    target_name: str,
    version: VersionInfo | None,
    namespaces: list[V1Namespace],
    nodes: list[V1Node],
    *,
    now: datetime | None = None,
) -> TopologyHints:
    """Assemble the full :class:`TopologyHints` for a Kubernetes cluster.

    Pure function over already-fetched API responses so the unit suite
    can drive it with synthetic :class:`V1Namespace` / :class:`V1Node`
    fixtures. The connector's
    :meth:`~KubernetesConnector.discover_topology` issues the three
    round-trips (``VersionApi.get_code()`` /
    ``CoreV1Api.list_namespace()`` / ``CoreV1Api.list_node()``) and
    hands the results in here.

    Namespaces or nodes with a missing ``metadata.name`` are skipped —
    we cannot emit a ``belongs-to`` edge for an object we can't
    identify, and the refresh service's natural key is
    ``(kind, name)``. The API server enforces ``metadata.name`` on
    every persisted object so this is defensive-only.

    ``now`` is parameterised for test-determinism (the
    :func:`age_seconds` derivation inside the row helpers); production
    callers leave it ``None`` and the helpers resolve
    :func:`datetime.now` at call time.
    """
    discovered_at = datetime.now(UTC) if now is None else now

    node_hints: list[NodeHint] = [build_target_node_hint(target_name, version)]
    edge_hints: list[EdgeHint] = []

    for ns in namespaces:
        if ns.metadata is None or not ns.metadata.name:
            continue
        node_hints.append(namespace_node_hint(ns, now=now))
        edge_hints.append(namespace_to_target_edge(ns.metadata.name, target_name))

    for n in nodes:
        if n.metadata is None or not n.metadata.name:
            continue
        node_hints.append(node_node_hint(n, now=now))
        edge_hints.append(node_to_target_edge(n.metadata.name, target_name))

    return TopologyHints(
        nodes=tuple(node_hints),
        edges=tuple(edge_hints),
        discovered_at=discovered_at,
    )
