# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the G9.1-T2 connector topology hooks and Pydantic schemas.

Coverage (per Task #449 acceptance criteria):

* ``Connector`` ABC exposes :meth:`Connector.discover_topology` and
  :meth:`Connector.list_candidates` as **non-abstract** methods with
  no-op defaults — every shipped v1 subclass that did not exist when
  these methods were added (VaultConnector #244, KubernetesConnector
  skeleton #321) must still instantiate without overriding them.
* Default :meth:`discover_topology` returns an empty
  :class:`TopologyHints` with ``discovered_at`` close to call time.
* Default :meth:`list_candidates` returns ``[]``.
* A subclass override of :meth:`discover_topology` returns the
  override's value via the ABC dispatch.
* :class:`NodeHint`, :class:`EdgeHint`, :class:`TopologyHints`, and
  :class:`CandidateHint` are frozen end-to-end: field reassignment
  raises :exc:`pydantic.ValidationError`; nested mappings raise
  :exc:`TypeError` on in-place mutation; round-tripping through
  ``model_dump`` / ``model_validate`` is lossless.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from meho_backplane.connectors import (
    CandidateHint,
    Connector,
    EdgeHint,
    FingerprintResult,
    NodeHint,
    OperationResult,
    ProbeResult,
    TopologyHints,
)
from meho_backplane.connectors.schemas import (
    CandidateHint as _CandidateDirect,
)
from meho_backplane.connectors.schemas import (
    EdgeHint as _EdgeDirect,
)
from meho_backplane.connectors.schemas import (
    NodeHint as _NodeDirect,
)
from meho_backplane.connectors.schemas import (
    TopologyHints as _TopologyDirect,
)

_NOW = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Package-level export checks (mirror test_connectors_base.py pattern)
# ---------------------------------------------------------------------------


def test_package_exports_topology_schemas() -> None:
    for cls in (NodeHint, EdgeHint, TopologyHints, CandidateHint):
        assert cls is not None


def test_package_root_aliases_match_submodule() -> None:
    assert NodeHint is _NodeDirect
    assert EdgeHint is _EdgeDirect
    assert TopologyHints is _TopologyDirect
    assert CandidateHint is _CandidateDirect


# ---------------------------------------------------------------------------
# Test subclasses (shared)
# ---------------------------------------------------------------------------


class _DefaultsConnector(Connector):
    """Subclass that inherits both topology defaults — exercises the no-op path."""

    product = "default-topology"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:
        raise NotImplementedError


class _OverridingConnector(Connector):
    """Subclass that overrides both topology methods — exercises dispatch."""

    product = "override-topology"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:
        raise NotImplementedError

    async def discover_topology(self, target: Any) -> TopologyHints:
        return TopologyHints(
            nodes=(
                NodeHint(kind="vm", name="vm-01"),
                NodeHint(kind="host", name="esxi-01"),
            ),
            edges=(
                EdgeHint(
                    from_kind="vm",
                    from_name="vm-01",
                    to_kind="host",
                    to_name="esxi-01",
                    kind="runs-on",
                ),
            ),
            discovered_at=_NOW,
        )

    async def list_candidates(self, seed_target: Any | None = None) -> list[CandidateHint]:
        return [
            CandidateHint(
                name="cluster-2",
                host="10.0.0.42",
                port=6443,
                evidence={"source": "kubeconfig", "context_name": "cluster-2"},
                confidence="high",
            ),
        ]


# ---------------------------------------------------------------------------
# Connector ABC — no-op defaults are non-abstract
# ---------------------------------------------------------------------------


def test_connector_defaults_subclass_instantiates_without_overrides() -> None:
    # The two new methods must NOT be abstract — otherwise existing v1
    # subclasses break. _DefaultsConnector overrides only fingerprint/
    # probe/execute and must remain instantiable.
    conn = _DefaultsConnector()
    assert conn.product == "default-topology"


def test_default_discover_topology_returns_empty_topology_hints() -> None:
    conn = _DefaultsConnector()
    before = datetime.now(UTC)
    result = asyncio.run(conn.discover_topology(target=None))
    after = datetime.now(UTC)
    assert isinstance(result, TopologyHints)
    assert result.nodes == ()
    assert result.edges == ()
    # discovered_at is stamped at call time, well inside [before, after].
    assert before - timedelta(seconds=1) <= result.discovered_at <= after + timedelta(seconds=1)
    assert result.discovered_at.tzinfo is UTC


def test_default_list_candidates_returns_empty_list() -> None:
    conn = _DefaultsConnector()
    result = asyncio.run(conn.list_candidates())
    assert result == []
    # The contract is "empty list" — a returned tuple would silently
    # break callers iterating with .append() before persisting.
    assert isinstance(result, list)


def test_default_list_candidates_accepts_seed_target() -> None:
    # The parameter is optional + plain ``Any | None`` — exercises both
    # the default (no arg) and the explicit-None call shapes.
    conn = _DefaultsConnector()
    result_default = asyncio.run(conn.list_candidates())
    result_none = asyncio.run(conn.list_candidates(seed_target=None))
    result_seeded = asyncio.run(conn.list_candidates(seed_target=object()))
    assert result_default == [] == result_none == result_seeded


# ---------------------------------------------------------------------------
# Connector ABC — overrides dispatch via the abstract base
# ---------------------------------------------------------------------------


def test_overridden_discover_topology_dispatches_via_abc() -> None:
    # Hold the binding as ``Connector`` — the dispatch must go through
    # the abstract base reference, proving the override is reachable
    # via the ABC contract (not just direct method call).
    conn: Connector = _OverridingConnector()
    result = asyncio.run(conn.discover_topology(target=None))
    assert isinstance(result, TopologyHints)
    assert len(result.nodes) == 2
    assert result.nodes[0].name == "vm-01"
    assert result.edges[0].kind == "runs-on"
    assert result.discovered_at == _NOW


def test_overridden_list_candidates_dispatches_via_abc() -> None:
    conn: Connector = _OverridingConnector()
    result = asyncio.run(conn.list_candidates(seed_target=None))
    assert len(result) == 1
    assert result[0].name == "cluster-2"
    assert result[0].confidence == "high"


# ---------------------------------------------------------------------------
# Shipped v1 subclasses inherit the new defaults unchanged
# ---------------------------------------------------------------------------


def test_shipped_v1_subclasses_inherit_topology_defaults() -> None:
    # Backward-compat guard: VaultConnector (#244) inherits the no-op
    # default for both ``discover_topology`` and ``list_candidates``;
    # ``KubernetesConnector`` (#321) inherits the ``list_candidates``
    # default. The K8s ``discover_topology`` override landed in
    # G0.14-T12 (#1201), so the "no override yet" invariant moved from
    # tested-fact to deleted line — see the populator at
    # :meth:`meho_backplane.connectors.kubernetes.connector.KubernetesConnector.discover_topology`
    # and the live coverage in ``test_connectors_k8s_topology.py``
    # (unit, synthetic V1Namespace/V1Node fixtures) +
    # ``tests/integration/test_connectors_k8s_k3d.py`` (k3s
    # testcontainer round-trip).
    from meho_backplane.connectors.kubernetes.connector import KubernetesConnector
    from meho_backplane.connectors.vault.connector import VaultConnector

    # Compare unbound class methods, not bound instance methods, so
    # we don't have to construct each connector (their constructors
    # touch settings + Vault).
    assert VaultConnector.discover_topology is Connector.discover_topology
    assert VaultConnector.list_candidates is Connector.list_candidates
    assert KubernetesConnector.list_candidates is Connector.list_candidates


# ---------------------------------------------------------------------------
# NodeHint
# ---------------------------------------------------------------------------


def test_node_hint_round_trip() -> None:
    nh = NodeHint(kind="vm", name="vm-01", properties={"power_state": "on"})
    dumped = nh.model_dump()
    restored = NodeHint.model_validate(dumped)
    assert restored == nh


def test_node_hint_default_properties_is_empty_mapping() -> None:
    nh = NodeHint(kind="host", name="esxi-01")
    assert dict(nh.properties) == {}


def test_node_hint_is_frozen() -> None:
    nh = NodeHint(kind="vm", name="vm-01")
    with pytest.raises(ValidationError):
        nh.name = "mutated"  # type: ignore[misc]


def test_node_hint_properties_is_deeply_immutable() -> None:
    nh = NodeHint(kind="vm", name="vm-01", properties={"k": "v"})
    with pytest.raises(TypeError):
        nh.properties["new_key"] = "value"  # type: ignore[index]


def test_node_hint_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        NodeHint(kind="not-a-real-kind", name="x")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# EdgeHint
# ---------------------------------------------------------------------------


def test_edge_hint_round_trip() -> None:
    eh = EdgeHint(
        from_kind="vm",
        from_name="vm-01",
        to_kind="host",
        to_name="esxi-01",
        kind="runs-on",
        properties={"since": "2026-05-14T00:00:00Z"},
    )
    dumped = eh.model_dump()
    restored = EdgeHint.model_validate(dumped)
    assert restored == eh


def test_edge_hint_default_properties_is_empty_mapping() -> None:
    eh = EdgeHint(
        from_kind="pod",
        from_name="p1",
        to_kind="namespace",
        to_name="ns-1",
        kind="belongs-to",
    )
    assert dict(eh.properties) == {}


def test_edge_hint_is_frozen() -> None:
    eh = EdgeHint(
        from_kind="vm",
        from_name="vm-01",
        to_kind="host",
        to_name="esxi-01",
        kind="runs-on",
    )
    with pytest.raises(ValidationError):
        eh.kind = "belongs-to"  # type: ignore[misc]


def test_edge_hint_properties_is_deeply_immutable() -> None:
    eh = EdgeHint(
        from_kind="vm",
        from_name="vm-01",
        to_kind="host",
        to_name="esxi-01",
        kind="runs-on",
        properties={"k": "v"},
    )
    with pytest.raises(TypeError):
        eh.properties["new_key"] = "value"  # type: ignore[index]


def test_edge_hint_rejects_unknown_edge_kind() -> None:
    with pytest.raises(ValidationError):
        EdgeHint(
            from_kind="vm",
            from_name="vm-01",
            to_kind="host",
            to_name="esxi-01",
            kind="authenticates-via",  # G9.2 territory — not yet allowed
        )  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# TopologyHints
# ---------------------------------------------------------------------------


def test_topology_hints_round_trip() -> None:
    th = TopologyHints(
        nodes=(NodeHint(kind="vm", name="vm-01"),),
        edges=(
            EdgeHint(
                from_kind="vm",
                from_name="vm-01",
                to_kind="host",
                to_name="esxi-01",
                kind="runs-on",
            ),
        ),
        discovered_at=_NOW,
    )
    dumped = th.model_dump()
    restored = TopologyHints.model_validate(dumped)
    assert restored == th


def test_topology_hints_accepts_list_inputs_and_converts_to_tuple() -> None:
    # Convenience for callers — Pydantic coerces ``list`` → ``tuple`` so
    # the discover_topology override doesn't have to pre-tuple the args.
    th = TopologyHints(
        nodes=[NodeHint(kind="vm", name="vm-01")],
        edges=[],
        discovered_at=_NOW,
    )
    assert isinstance(th.nodes, tuple)
    assert isinstance(th.edges, tuple)
    assert len(th.nodes) == 1


def test_topology_hints_defaults_are_empty_tuples() -> None:
    th = TopologyHints(discovered_at=_NOW)
    assert th.nodes == ()
    assert th.edges == ()


def test_topology_hints_is_frozen() -> None:
    th = TopologyHints(discovered_at=_NOW)
    with pytest.raises(ValidationError):
        th.discovered_at = datetime.now(UTC)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# CandidateHint
# ---------------------------------------------------------------------------


def test_candidate_hint_round_trip() -> None:
    ch = CandidateHint(
        name="cluster-2",
        host="10.0.0.42",
        port=6443,
        evidence={"source": "kubeconfig", "context_name": "cluster-2"},
        confidence="high",
    )
    dumped = ch.model_dump()
    restored = CandidateHint.model_validate(dumped)
    assert restored == ch


def test_candidate_hint_optional_port_defaults_to_none() -> None:
    ch = CandidateHint(
        name="esxi-09",
        host="10.0.0.99",
        evidence={"source": "vcenter.host.list"},
        confidence="medium",
    )
    assert ch.port is None


def test_candidate_hint_is_frozen() -> None:
    ch = CandidateHint(
        name="x",
        host="h",
        evidence={"src": "test"},
        confidence="low",
    )
    with pytest.raises(ValidationError):
        ch.name = "mutated"  # type: ignore[misc]


def test_candidate_hint_evidence_is_deeply_immutable() -> None:
    ch = CandidateHint(
        name="x",
        host="h",
        evidence={"src": "test"},
        confidence="low",
    )
    with pytest.raises(TypeError):
        ch.evidence["new_key"] = "value"  # type: ignore[index]


def test_candidate_hint_rejects_unknown_confidence() -> None:
    with pytest.raises(ValidationError):
        CandidateHint(
            name="x",
            host="h",
            evidence={},
            confidence="certain",  # not in {high, medium, low}
        )  # type: ignore[arg-type]
