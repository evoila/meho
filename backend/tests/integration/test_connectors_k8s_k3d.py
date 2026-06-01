# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Integration test for :class:`KubernetesConnector` against a real k3s cluster.

Boots a single ``rancher/k3s`` container via
:class:`testcontainers.k3s.K3SContainer`, pulls the kubeconfig YAML it
exposes, parses it, and exercises the live ``fingerprint`` /
``probe`` / ``_get_api_client`` / ``aclose`` paths against the running
API server.

Skip conditions:

* Docker socket missing — same heuristic the rest of the
  ``tests/integration/`` package uses.
* k3s container start fails (privileged not allowed, cgroup v1 host
  refusing the v2 mount, etc.) — surfaces as a clean skip rather than
  a hard failure so a Docker-having-but-not-k3s-having sandbox isn't
  flagged red.

CI side: the runner provisions Docker; the integration job (when the
Initiative's CI wiring lands under T6) sets
``MEHO_TEST_K3S_IMAGE`` to a registry-mirrored k3s image if needed.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.kubernetes import (
    KubernetesConnector,
    KubernetesTargetLike,
    parse_kubeconfig_yaml,
)

# ---------------------------------------------------------------------------
# Docker availability — mirrors tests/integration/conftest.py heuristic
# ---------------------------------------------------------------------------


def _docker_socket_present() -> bool:
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


DOCKER_AVAILABLE: bool = _docker_socket_present()
SKIP_REASON: str = (
    "Docker socket unavailable in this sandbox; runs in CI where containers are provisioned."
)


# ---------------------------------------------------------------------------
# Target stub — minimal shape KubernetesConnector reads from
# ---------------------------------------------------------------------------


@dataclass
class _K3sTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str


# ---------------------------------------------------------------------------
# k3s container fixture — module-scoped (one boot, multiple tests)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def k3s_kubeconfig_and_target() -> Any:
    """Boot a k3s container; yield (kubeconfig_dict, target stub).

    Container shut down on fixture teardown. The kubeconfig the
    container exposes points to the container's host-mapped TLS port,
    so :meth:`fingerprint` actually talks to the running API server.
    """
    if not DOCKER_AVAILABLE:
        pytest.skip(SKIP_REASON)

    # Local import — testcontainers transitively imports the docker SDK
    # which probes the socket on import. Keeping the import inside the
    # fixture lets the module collect on a no-Docker sandbox.
    try:
        from testcontainers.k3s import K3SContainer
    except ImportError as exc:  # pragma: no cover — testcontainers ships k3s in 4.x
        pytest.skip(f"testcontainers.k3s unavailable: {exc}")

    # Default tag pinned to a known-good k3s minor aligned with
    # ``kubernetes_asyncio>=32,<33`` (both target the K8s 1.32 API).
    # ``rancher/k3s:latest`` would let an upstream release with a
    # regression in ``/readyz`` / ``/version`` / auth break CI on PRs
    # that did not touch the connector — same rationale the rest of
    # the integration suite already follows for ``pgvector/pgvector``
    # (``MEHO_TEST_PGVECTOR_IMAGE`` defaults to a pinned minor, not
    # ``:latest``). ``MEHO_TEST_K3S_IMAGE`` override stays for a
    # registry-mirror swap or a deliberate version bump.
    image = os.environ.get("MEHO_TEST_K3S_IMAGE", "rancher/k3s:v1.32.5-k3s1")
    try:
        container = K3SContainer(image=image)
        container.start()
    except Exception as exc:
        pytest.skip(f"k3s container failed to start ({type(exc).__name__}): {exc}")

    try:
        kubeconfig_text = container.config_yaml()
        kubeconfig = parse_kubeconfig_yaml(kubeconfig_text)
        # The kubeconfig's ``server`` URL points at the host-mapped TLS
        # port — extract host + port so the probe (which is
        # kubeconfig-free) can hit the same endpoint.
        server_url = kubeconfig["clusters"][0]["cluster"]["server"]
        parsed = urlparse(server_url)
        target = _K3sTarget(
            name="k3s-test",
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port,
            secret_ref="k8s/k3s-test",
        )
        yield kubeconfig, target
    finally:
        container.stop()


def _make_k3d_operator() -> Operator:
    """Build a non-system operator carrying a non-empty ``raw_jwt``.

    G3.10-T4 (#948) added ``operator`` to every typed handler's
    signature; the injected loader here ignores it (the test container's
    kubeconfig is passed through verbatim), but the handler forwards it
    to :meth:`KubernetesConnector._get_api_client`.
    """
    import uuid as _uuid

    return Operator(
        sub="op-k3d-test",
        name="K3d Test Operator",
        email=None,
        raw_jwt="op.k3d.jwt",
        tenant_id=_uuid.UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


@pytest.fixture
async def k3s_connector(
    k3s_kubeconfig_and_target: tuple[dict[str, Any], _K3sTarget],
) -> AsyncIterator[tuple[KubernetesConnector, _K3sTarget]]:
    """Yield a connector whose loader returns the container's kubeconfig."""
    kubeconfig, target = k3s_kubeconfig_and_target

    async def _loader(_target: KubernetesTargetLike, _operator: Operator) -> dict[str, Any]:
        return kubeconfig

    connector = KubernetesConnector(kubeconfig_loader=_loader)
    try:
        yield connector, target
    finally:
        await connector.aclose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_against_k3s_returns_product_k3s(
    k3s_connector: tuple[KubernetesConnector, _K3sTarget],
) -> None:
    """Live fingerprint maps to product=k3s and populates version."""
    connector, target = k3s_connector
    result = await connector.fingerprint(target)
    assert result.vendor == "kubernetes"
    assert result.product == "k3s", (
        f"k3s container reported gitVersion={result.version!r}; "
        f"product_from_git_version returned {result.product!r}"
    )
    assert result.version and result.version.startswith("v")
    assert result.reachable is True
    assert result.probe_method == "GET /version"
    # Every extras key the connector populates is filled by the live
    # API; spot-check the load-bearing ones (others can be empty in
    # some k3s builds).
    assert "major" in result.extras
    assert "minor" in result.extras
    assert "platform" in result.extras


@pytest.mark.asyncio
async def test_probe_against_running_k3s_returns_ok(
    k3s_connector: tuple[KubernetesConnector, _K3sTarget],
) -> None:
    """The kubeconfig-free probe reaches the live k3s ``/readyz``."""
    connector, target = k3s_connector
    result = await connector.probe(target)
    assert result.ok is True, f"probe returned not-ok: reason={result.reason!r}"
    assert result.latency_ms is not None and result.latency_ms >= 0.0


@pytest.mark.asyncio
async def test_probe_against_unreachable_host_returns_not_ok(
    k3s_kubeconfig_and_target: tuple[dict[str, Any], _K3sTarget],
) -> None:
    """An invalid host yields an informative ``ok=False`` reason."""
    kubeconfig, _ = k3s_kubeconfig_and_target

    async def _loader(_target: KubernetesTargetLike, _operator: Operator) -> dict[str, Any]:
        return kubeconfig

    bogus = _K3sTarget(
        name="unreachable",
        host="127.0.0.1",
        port=1,  # nothing listens on TCP/1; probe must surface a clear failure
        secret_ref="",
    )
    connector = KubernetesConnector(kubeconfig_loader=_loader)
    try:
        result = await connector.probe(bogus)
    finally:
        await connector.aclose()
    assert result.ok is False
    assert result.reason is not None


@pytest.mark.asyncio
async def test_api_client_cached_across_calls_against_live_k3s(
    k3s_connector: tuple[KubernetesConnector, _K3sTarget],
) -> None:
    """Second fingerprint against the same target reuses the cached client."""
    connector, target = k3s_connector
    # Derive the cache key from the SUT itself so the test tracks the
    # connector's keying contract (currently ``target.secret_ref``;
    # see ``KubernetesConnector._cache_key``). Indexing by
    # ``target.name`` instead raised ``KeyError`` whenever the k3s
    # testcontainer actually provisioned.
    cache_key = connector._cache_key(target)
    await connector.fingerprint(target)
    cached_client_id_after_first = id(connector._api_clients[cache_key])
    await connector.fingerprint(target)
    cached_client_id_after_second = id(connector._api_clients[cache_key])
    assert cached_client_id_after_first == cached_client_id_after_second


# ---------------------------------------------------------------------------
# G3.2-T2 (#322) core inventory ops -- live k3s exercise.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_namespace_list_against_k3s_returns_default_set(
    k3s_connector: tuple[KubernetesConnector, _K3sTarget],
) -> None:
    """``k8s.namespace.list`` over k3s returns the bootstrap namespaces."""
    connector, target = k3s_connector
    result = await connector.k8s_namespace_list(_make_k3d_operator(), target, {})
    assert result["total"] >= 4  # default, kube-system, kube-public, kube-node-lease
    names = {row["name"] for row in result["rows"]}
    assert "default" in names
    assert "kube-system" in names
    # Phase is Active for the bootstrap set; any other phase is a real
    # signal worth surfacing to the operator.
    for row in result["rows"]:
        assert row["status"] in {"Active", "Terminating"}


@pytest.mark.asyncio
async def test_node_list_against_k3s_returns_at_least_one_node(
    k3s_connector: tuple[KubernetesConnector, _K3sTarget],
) -> None:
    """``k8s.node.list`` over k3s returns the single-node default cluster."""
    connector, target = k3s_connector
    result = await connector.k8s_node_list(_make_k3d_operator(), target, {})
    assert result["total"] >= 1
    node = result["rows"][0]
    # k3s nodes report Ready=True post-boot; the test waits implicitly via
    # the container fixture's start barrier (testcontainers blocks until
    # the k3s API server's ``/readyz`` returns 200), so a NotReady reading
    # here is a real signal, not flake.
    assert node["status"] == "Ready"
    # kubelet version surfaces verbatim; spot-check it's a v-prefixed
    # SemVer-shaped string.
    assert node["version"].startswith("v")
    # k3s lights up the master / control-plane / etcd role labels.
    assert node["roles"], "k3s nodes always carry at least one role label"


@pytest.mark.asyncio
async def test_ls_root_against_k3s_returns_namespaces_and_cluster_kinds(
    k3s_connector: tuple[KubernetesConnector, _K3sTarget],
) -> None:
    """``k8s.ls /`` over k3s returns the live namespace set + the fixed cluster kinds."""
    connector, target = k3s_connector
    result = await connector.k8s_ls(_make_k3d_operator(), target, {"path": "/"})
    assert result["path"] == "/"
    assert "default" in result["namespaces"]
    assert "kube-system" in result["namespaces"]
    assert "nodes" in result["cluster_kinds"]
    assert "namespaces" in result["cluster_kinds"]


@pytest.mark.asyncio
async def test_ls_namespace_against_k3s_returns_kind_counts(
    k3s_connector: tuple[KubernetesConnector, _K3sTarget],
) -> None:
    """``k8s.ls /kube-system`` returns per-kind counts via remaining_item_count."""
    connector, target = k3s_connector
    result = await connector.k8s_ls(_make_k3d_operator(), target, {"path": "/kube-system"})
    assert result["namespace"] == "kube-system"
    assert result["cluster_kinds_omitted"] is True
    kinds_by_label = {entry["kind"]: entry for entry in result["kinds"]}
    # k3s ships with pods + services + configmaps in kube-system from the
    # first boot. Assertions are existential, not exact counts -- the
    # exact counts drift across k3s minors and aren't load-bearing.
    assert kinds_by_label["pods"]["count"] is not None
    assert kinds_by_label["pods"]["count"] >= 1
    assert kinds_by_label["services"]["count"] is not None
    assert kinds_by_label["services"]["count"] >= 1


@pytest.mark.asyncio
async def test_ls_namespace_kind_against_k3s_forwards_to_unknown_op(
    k3s_connector: tuple[KubernetesConnector, _K3sTarget],
) -> None:
    """``k8s.ls /default/pods`` forwards to ``k8s.pod.list``.

    Pre-T3, this exercised the dispatcher's ``unknown_op`` shape.
    T3 (#323) registers ``k8s.pod.list``, but registration only runs
    inside the lifespan-driven
    :meth:`KubernetesConnector.register_operations`; this test
    constructs the connector directly, so no descriptor row is written
    to ``endpoint_descriptor`` and the dispatcher shim still surfaces
    ``unknown_op`` on the forwarded sub-call. Live registration +
    end-to-end dispatch is covered by the chassis lifespan tests, not
    by this module.
    """
    connector, target = k3s_connector
    result = await connector.k8s_ls(_make_k3d_operator(), target, {"path": "/default/pods"})
    assert result["forwarded_to"] == "k8s.pod.list"
    inner = result["result"]
    assert inner["status"] == "error"
    assert inner["error"].startswith("unknown_op:")


# ---------------------------------------------------------------------------
# G3.2-T3 (#323) workload ops -- live k3s exercise.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pod_list_against_k3s_returns_kube_system_pods(
    k3s_connector: tuple[KubernetesConnector, _K3sTarget],
) -> None:
    """``k8s.pod.list -n kube-system`` returns the bootstrap pods.

    k3s ships coredns + traefik + local-path-provisioner + metrics-server
    pods in ``kube-system`` post-boot; the assertion is existential
    (at least one pod) rather than exact-list because the k3s minor
    drift adds/removes shipped components.
    """
    connector, target = k3s_connector
    result = await connector.k8s_pod_list(
        _make_k3d_operator(), target, {"namespace": "kube-system"}
    )
    assert result["total"] >= 1
    sample = result["rows"][0]
    for key in ("name", "namespace", "status", "ready", "restarts", "age_seconds", "node", "ip"):
        assert key in sample
    assert sample["namespace"] == "kube-system"


@pytest.mark.asyncio
async def test_pod_list_all_namespaces_against_k3s(
    k3s_connector: tuple[KubernetesConnector, _K3sTarget],
) -> None:
    """``k8s.pod.list --all-namespaces`` returns pods spanning multiple namespaces."""
    connector, target = k3s_connector
    result = await connector.k8s_pod_list(_make_k3d_operator(), target, {"all_namespaces": True})
    assert result["total"] >= 1
    namespaces = {row["namespace"] for row in result["rows"]}
    # k3s' bootstrap pods live in kube-system.
    assert "kube-system" in namespaces


@pytest.mark.asyncio
async def test_pod_list_server_side_limit_caps_rows(
    k3s_connector: tuple[KubernetesConnector, _K3sTarget],
) -> None:
    """``limit=1`` enforces a one-row page; further pages reachable via continue_token."""
    connector, target = k3s_connector
    result = await connector.k8s_pod_list(
        _make_k3d_operator(), target, {"namespace": "kube-system", "limit": 1}
    )
    assert result["total"] == 1
    # next_continue presence depends on whether kube-system has >1 pod
    # (it normally does on k3s); single-pod namespaces omit the token.
    # Both shapes are valid; when present, the cursor round-trips
    # cleanly back through a second list call.
    if "next_continue" in result:
        page_2 = await connector.k8s_pod_list(
            _make_k3d_operator(),
            target,
            {
                "namespace": "kube-system",
                "limit": 1,
                "continue_token": result["next_continue"],
            },
        )
        assert page_2["total"] >= 0


@pytest.mark.asyncio
async def test_pod_info_against_k3s_resolves_prefix(
    k3s_connector: tuple[KubernetesConnector, _K3sTarget],
) -> None:
    """``k8s.pod.info`` resolves a unique prefix and returns full container detail."""
    connector, target = k3s_connector
    listing = await connector.k8s_pod_list(
        _make_k3d_operator(), target, {"namespace": "kube-system"}
    )
    assert listing["total"] >= 1
    pod_name = listing["rows"][0]["name"]
    # k3s pod names follow ``<deployment>-<hash>-<id>``; the first
    # segment is unique when only one pod from a given deployment
    # exists. Fall back to the full name when the prefix would clash.
    prefix = pod_name.split("-", 1)[0]
    prefix_matches = [r for r in listing["rows"] if r["name"].startswith(prefix)]
    target_name = prefix if len(prefix_matches) == 1 else pod_name
    info = await connector.k8s_pod_info(
        _make_k3d_operator(), target, {"pod_name": target_name, "namespace": "kube-system"}
    )
    assert info["name"] == pod_name
    assert info["namespace"] == "kube-system"
    assert isinstance(info["containers"], list)
    assert len(info["containers"]) >= 1


@pytest.mark.asyncio
async def test_pod_info_not_found_raises_against_k3s(
    k3s_connector: tuple[KubernetesConnector, _K3sTarget],
) -> None:
    """A non-existent pod name surfaces :class:`WorkloadNotFoundError`."""
    from meho_backplane.connectors.kubernetes.ops_workload import WorkloadNotFoundError

    connector, target = k3s_connector
    with pytest.raises(WorkloadNotFoundError):
        await connector.k8s_pod_info(
            _make_k3d_operator(),
            target,
            {"pod_name": "definitely-not-a-real-pod-zzz", "namespace": "kube-system"},
        )


@pytest.mark.asyncio
async def test_deployment_list_against_k3s_kube_system(
    k3s_connector: tuple[KubernetesConnector, _K3sTarget],
) -> None:
    """``k8s.deployment.list -n kube-system`` returns the bootstrap deployments.

    k3s ships coredns + metrics-server + local-path-provisioner +
    traefik as deployments in ``kube-system``; this assertion is
    existential (at least one) for k3s-minor drift tolerance.
    """
    connector, target = k3s_connector
    result = await connector.k8s_deployment_list(
        _make_k3d_operator(), target, {"namespace": "kube-system"}
    )
    assert result["total"] >= 1
    sample = result["rows"][0]
    for key in (
        "name",
        "namespace",
        "replicas_desired",
        "replicas_ready",
        "replicas_available",
        "image",
        "age_seconds",
        "strategy",
    ):
        assert key in sample
    assert sample["namespace"] == "kube-system"


@pytest.mark.asyncio
async def test_deployment_info_against_k3s_returns_full_detail(
    k3s_connector: tuple[KubernetesConnector, _K3sTarget],
) -> None:
    """``k8s.deployment.info`` returns the full template + status block."""
    connector, target = k3s_connector
    listing = await connector.k8s_deployment_list(
        _make_k3d_operator(), target, {"namespace": "kube-system"}
    )
    assert listing["total"] >= 1
    dep_name = listing["rows"][0]["name"]
    info = await connector.k8s_deployment_info(
        _make_k3d_operator(), target, {"deployment_name": dep_name, "namespace": "kube-system"}
    )
    assert info["name"] == dep_name
    assert info["namespace"] == "kube-system"
    assert "status" in info
    assert "containers" in info
    assert len(info["containers"]) >= 1


# ---------------------------------------------------------------------------
# G3.2-T4 (#324) network + config + event ops -- live k3s exercise.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_list_against_k3s_returns_kubernetes_service(
    k3s_connector: tuple[KubernetesConnector, _K3sTarget],
) -> None:
    """``k8s.service.list --namespace default`` returns the bootstrap ``kubernetes`` service."""
    connector, target = k3s_connector
    result = await connector.k8s_service_list(
        _make_k3d_operator(), target, {"namespace": "default"}
    )
    # Every k3s cluster ships with the ``kubernetes`` ClusterIP service
    # in the ``default`` namespace -- it's the API server's in-cluster
    # endpoint. Use it as the existence assertion; specific cluster_ip
    # values vary across boots so don't pin those.
    names = {row["name"] for row in result["rows"]}
    assert "kubernetes" in names
    kube_svc = next(row for row in result["rows"] if row["name"] == "kubernetes")
    assert kube_svc["type"] == "ClusterIP"
    assert kube_svc["cluster_ip"]
    assert any(port["port"] == 443 for port in kube_svc["ports"])


@pytest.mark.asyncio
async def test_ingress_list_against_k3s_returns_empty_or_default(
    k3s_connector: tuple[KubernetesConnector, _K3sTarget],
) -> None:
    """``k8s.ingress.list --namespace default`` returns the (likely-empty) ingress list.

    k3s ships Traefik in ``kube-system`` but no ingresses by default in
    ``default``. The assertion is structural -- the call succeeds and
    returns the expected envelope shape -- not numeric. A list-op that
    failed against a real cluster would surface as an exception here.
    """
    connector, target = k3s_connector
    result = await connector.k8s_ingress_list(
        _make_k3d_operator(), target, {"namespace": "default"}
    )
    assert isinstance(result["rows"], list)
    assert result["total"] == len(result["rows"])


@pytest.mark.asyncio
async def test_configmap_list_against_k3s_keys_only_data_absent(
    k3s_connector: tuple[KubernetesConnector, _K3sTarget],
) -> None:
    """``k8s.configmap.list --namespace kube-system`` returns keys-only rows; data absent.

    The privacy contract pinned by the unit tests is re-asserted here
    against the live cluster: even with real configmaps in
    ``kube-system`` (k3s ships several including ``coredns`` and
    ``local-path-config``), the list-op rows MUST NOT carry ``data``
    or ``binary_data``.
    """
    connector, target = k3s_connector
    result = await connector.k8s_configmap_list(
        _make_k3d_operator(), target, {"namespace": "kube-system"}
    )
    assert result["total"] >= 1, "k8s kube-system should ship at least one configmap"
    for row in result["rows"]:
        # Privacy contract: keys populated, values absent.
        assert "keys" in row
        assert "data" not in row
        assert "binary_data" not in row


@pytest.mark.asyncio
async def test_configmap_info_against_k3s_returns_full_data(
    k3s_connector: tuple[KubernetesConnector, _K3sTarget],
) -> None:
    """``k8s.configmap.info`` returns full data + binary_data for a known configmap.

    Picks a configmap from ``k8s.configmap.list`` and re-reads it via
    ``info``; the test is independent of which specific configmap k3s
    happens to ship in this version.
    """
    connector, target = k3s_connector
    list_result = await connector.k8s_configmap_list(
        _make_k3d_operator(), target, {"namespace": "kube-system"}
    )
    assert list_result["rows"], "need at least one configmap to exercise info"
    first = list_result["rows"][0]
    info_result = await connector.k8s_configmap_info(
        _make_k3d_operator(), target, {"name": first["name"], "namespace": "kube-system"}
    )
    assert info_result["name"] == first["name"]
    assert info_result["namespace"] == "kube-system"
    # ``data`` is a dict (possibly empty if the configmap has only
    # binary_data); ``metadata.labels`` / ``metadata.annotations`` are
    # always-present dicts in the wire shape.
    assert isinstance(info_result["data"], dict)
    assert isinstance(info_result["binary_data"], dict)
    assert isinstance(info_result["metadata"]["labels"], dict)


@pytest.mark.asyncio
async def test_event_list_against_k3s_returns_namespaced_events(
    k3s_connector: tuple[KubernetesConnector, _K3sTarget],
) -> None:
    """``k8s.event.list --namespace kube-system`` returns recent events sorted recent-first."""
    connector, target = k3s_connector
    result = await connector.k8s_event_list(
        _make_k3d_operator(), target, {"namespace": "kube-system", "limit": 50}
    )
    # k3s emits events during boot (pod scheduling, image pulls, leader
    # election); the kube-system namespace always has some.
    assert isinstance(result["rows"], list)
    # Ordering is most-recent-first by ``last_seen_seconds`` (smaller
    # value = more recent).
    seen = [
        row["last_seen_seconds"] for row in result["rows"] if row["last_seen_seconds"] is not None
    ]
    assert seen == sorted(seen)


@pytest.mark.asyncio
async def test_event_list_against_k3s_field_selector_filters(
    k3s_connector: tuple[KubernetesConnector, _K3sTarget],
) -> None:
    """``--field-selector type=Warning`` returns only Warning events.

    May return zero rows on a healthy cluster -- the assertion is that
    every returned row matches the filter, not that any rows exist.
    """
    connector, target = k3s_connector
    result = await connector.k8s_event_list(
        _make_k3d_operator(),
        target,
        {"namespace": "kube-system", "field_selector": "type=Warning"},
    )
    assert isinstance(result["rows"], list)
    for row in result["rows"]:
        assert row["type"] == "Warning"


# ---------------------------------------------------------------------------
# G0.14-T12 (#1201) discover_topology populator -- live k3s exercise.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_topology_against_k3s_returns_cluster_namespaces_and_nodes(
    k3s_connector: tuple[KubernetesConnector, _K3sTarget],
) -> None:
    """``discover_topology`` over live k3s emits target + namespace + node hints.

    The populator scope is deliberately minimal: 1 ``target`` NodeHint
    for the cluster (with the server version on properties), N
    ``namespace`` NodeHints, M ``node`` NodeHints, and ``belongs-to``
    edges from every namespace and every cluster node to the target.
    Pods / services / ingresses / deployments / volumes are out of
    scope at v0.7 — sibling Tasks under Initiative #1139 land them
    when refresh-cost data justifies the spend.
    """
    from meho_backplane.connectors.schemas import TopologyHints

    connector, target = k3s_connector
    hints = await connector.discover_topology(target, operator=_make_k3d_operator())

    assert isinstance(hints, TopologyHints)

    target_hints = [n for n in hints.nodes if n.kind == "target"]
    namespace_hints = [n for n in hints.nodes if n.kind == "namespace"]
    node_hints = [n for n in hints.nodes if n.kind == "node"]

    # Exactly one cluster anchor.
    assert len(target_hints) == 1
    assert target_hints[0].name == target.name
    # Server version surfaces verbatim on the target node's properties
    # so an operator inspecting the graph can identify the cluster.
    assert target_hints[0].properties["git_version"].startswith("v")

    # k3s ships ``default`` / ``kube-system`` / ``kube-public`` /
    # ``kube-node-lease`` from boot.
    namespace_names = {n.name for n in namespace_hints}
    assert "default" in namespace_names
    assert "kube-system" in namespace_names
    assert len(namespace_hints) >= 4

    # Single-node default k3s cluster.
    assert len(node_hints) >= 1

    # Every namespace and every cluster node carries a belongs-to edge
    # to the target.
    namespace_edges = [
        e for e in hints.edges if e.from_kind == "namespace" and e.to_kind == "target"
    ]
    node_edges = [e for e in hints.edges if e.from_kind == "node" and e.to_kind == "target"]
    assert {e.from_name for e in namespace_edges} == namespace_names
    assert {e.from_name for e in node_edges} == {n.name for n in node_hints}
    assert all(e.kind == "belongs-to" for e in hints.edges)
    assert all(e.to_name == target.name for e in hints.edges)


@pytest.mark.asyncio
async def test_discover_topology_against_k3s_is_idempotent_when_recalled(
    k3s_connector: tuple[KubernetesConnector, _K3sTarget],
) -> None:
    """Two back-to-back populator calls return the same node/edge keys.

    The refresh service's diff/apply sweep depends on stable
    ``(kind, name)`` natural keys across snapshots; this test pins
    that against the live API server (creation_timestamp-derived
    ``age_seconds`` properties drift but the key tuples don't).
    """
    connector, target = k3s_connector
    snap1 = await connector.discover_topology(target, operator=_make_k3d_operator())
    snap2 = await connector.discover_topology(target, operator=_make_k3d_operator())

    keys1 = {(n.kind, n.name) for n in snap1.nodes}
    keys2 = {(n.kind, n.name) for n in snap2.nodes}
    assert keys1 == keys2

    edge_keys1 = {(e.from_kind, e.from_name, e.to_kind, e.to_name, e.kind) for e in snap1.edges}
    edge_keys2 = {(e.from_kind, e.from_name, e.to_kind, e.to_name, e.kind) for e in snap2.edges}
    assert edge_keys1 == edge_keys2


# ---------------------------------------------------------------------------
# G3.14-T2 (#1404) k8s.exec -- live k3s exercise over the WsApiClient
# websocket transport. Creates a short-lived busybox pod (which ships a
# real shell, unlike the distroless k3s system pods), execs against it,
# and deletes it on teardown.
# ---------------------------------------------------------------------------


@pytest.fixture
async def busybox_pod(
    k3s_connector: tuple[KubernetesConnector, _K3sTarget],
) -> AsyncIterator[tuple[KubernetesConnector, _K3sTarget, str]]:
    """Create a running busybox pod in ``default``; yield its name; delete it.

    busybox is used because the k3s system pods (coredns, traefik,
    metrics-server) run distroless / scratch images with no shell, so
    exec'ing ``sh -c`` against them fails. The pod runs ``sleep`` so it
    stays Running for the duration of the exec calls.
    """
    import asyncio as _asyncio

    from kubernetes_asyncio import client as _client

    connector, target = k3s_connector
    operator = _make_k3d_operator()
    api_client = await connector._get_api_client(target, operator)
    v1 = _client.CoreV1Api(api_client)

    namespace = "default"
    # Server-generated name (generateName) + captured created.metadata.name:
    # delete_namespaced_pod is async, so a fixed name races AlreadyExists
    # across the three function-scoped exec tests on re-run / in parallel.
    pod_manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"generateName": "meho-exec-it-busybox-", "namespace": namespace},
        "spec": {
            "restartPolicy": "Never",
            "containers": [
                {
                    "name": "busybox",
                    "image": os.environ.get("MEHO_TEST_BUSYBOX_IMAGE", "busybox:1.36"),
                    "command": ["sleep", "600"],
                }
            ],
        },
    }
    created = await v1.create_namespaced_pod(namespace=namespace, body=pod_manifest)
    pod_name = created.metadata.name

    # Wait for the pod to reach Running (image pull + start). The
    # container fixture already gated on the API server's readiness, so
    # this loop only waits on the busybox image pull.
    for _ in range(60):
        pod = await v1.read_namespaced_pod(name=pod_name, namespace=namespace)
        if pod.status is not None and pod.status.phase == "Running":
            break
        await _asyncio.sleep(1.0)
    else:
        await v1.delete_namespaced_pod(name=pod_name, namespace=namespace)
        pytest.skip("busybox pod did not reach Running within 60s")

    try:
        yield connector, target, pod_name
    finally:
        # Best-effort teardown -- a delete failure (API blip) should not
        # mask the test result; the pod is restartPolicy=Never.
        with contextlib.suppress(Exception):
            await v1.delete_namespaced_pod(name=pod_name, namespace=namespace)


@pytest.mark.asyncio
async def test_exec_against_k3s_captures_stdout_and_exit_zero(
    busybox_pod: tuple[KubernetesConnector, _K3sTarget, str],
) -> None:
    """``k8s.exec echo`` over the live websocket returns stdout + exit 0."""
    connector, target, pod_name = busybox_pod
    result = await connector.exec_command(
        _make_k3d_operator(),
        target,
        {
            "pod_name": pod_name,
            "namespace": "default",
            "command": ["echo", "hello-from-exec"],
        },
    )
    assert result["pod"] == pod_name
    assert result["container"] == "busybox"
    assert result["stdout"] == "hello-from-exec\n"
    assert result["stderr"] == ""
    assert result["exit_code"] == 0
    assert result["timed_out"] is False
    assert result["truncated"] is False


@pytest.mark.asyncio
async def test_exec_against_k3s_non_zero_exit_parsed_from_status_frame(
    busybox_pod: tuple[KubernetesConnector, _K3sTarget, str],
) -> None:
    """A command that exits non-zero surfaces the code from the status frame."""
    connector, target, pod_name = busybox_pod
    result = await connector.exec_command(
        _make_k3d_operator(),
        target,
        {
            "pod_name": pod_name,
            "namespace": "default",
            "command": ["sh", "-c", "echo to-stderr 1>&2; exit 7"],
        },
    )
    assert result["exit_code"] == 7
    assert "to-stderr" in result["stderr"]
    assert result["timed_out"] is False


@pytest.mark.asyncio
async def test_exec_against_k3s_timeout_returns_partial_and_timed_out(
    busybox_pod: tuple[KubernetesConnector, _K3sTarget, str],
) -> None:
    """A long-running command is cut off at the deadline with timed_out=true."""
    connector, target, pod_name = busybox_pod
    result = await connector.exec_command(
        _make_k3d_operator(),
        target,
        {
            "pod_name": pod_name,
            "namespace": "default",
            "command": ["sh", "-c", "echo started; sleep 30"],
            "timeout_seconds": 2,
        },
    )
    assert result["timed_out"] is True
    assert result["exit_code"] is None
    # The "started" line is emitted immediately, before the sleep.
    assert "started" in result["stdout"]


# ---------------------------------------------------------------------------
# G3.14-T1 (#1403) single-call write ops -- live k3s exercise.
#
# These talk to the real API server: they create + mutate + delete real
# objects, so each test cleans up the namespace it touches. The redaction
# assertions cross-check classify_op/redact_payload (the #1401 broadcast
# contract) against the live secret/job bodies.
# ---------------------------------------------------------------------------


@pytest.fixture
async def write_namespace(
    k3s_connector: tuple[KubernetesConnector, _K3sTarget],
) -> Any:
    """Create a unique scratch namespace via ``k8s.namespace.create`` and tear it down.

    The namespace name carries a per-run random suffix so a leftover from a
    prior run (or a retry, or test-ordering inside the same session) can't
    pre-exist and make the idempotent ``k8s.namespace.create`` report
    ``created=False`` on this first creation. Each test gets the name via the
    yielded tuple rather than a module-level constant.
    """
    import uuid as _uuid

    from kubernetes_asyncio import client as _client

    connector, target = k3s_connector
    op = _make_k3d_operator()
    ns_name = f"meho-write-test-{_uuid.uuid4().hex[:8]}"
    created = await connector.k8s_namespace_create(op, target, {"name": ns_name})
    assert created["created"] is True
    try:
        yield connector, target, op, ns_name
    finally:
        import contextlib

        api_client = await connector._get_api_client(target, op)
        core_v1 = _client.CoreV1Api(api_client)
        with contextlib.suppress(Exception):
            await core_v1.delete_namespace(name=ns_name)


@pytest.mark.asyncio
async def test_namespace_create_is_idempotent_against_k3s(
    write_namespace: Any,
) -> None:
    """A second create against the existing namespace reports created=False."""
    connector, target, op, ns_name = write_namespace
    again = await connector.k8s_namespace_create(op, target, {"name": ns_name})
    assert again["created"] is False
    assert again["already_existed"] is True


@pytest.mark.asyncio
async def test_apply_server_dry_run_then_real_against_k3s(
    write_namespace: Any,
) -> None:
    """Server-dry-run previews without persisting; the real apply creates it."""
    from kubernetes_asyncio import client as _client

    connector, target, op, ns_name = write_namespace
    manifest = (
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "metadata:\n"
        f"  name: nginx\n  namespace: {ns_name}\n"
        "spec:\n"
        "  replicas: 1\n"
        "  selector:\n    matchLabels:\n      app: nginx\n"
        "  template:\n"
        "    metadata:\n      labels:\n        app: nginx\n"
        "    spec:\n      containers:\n      - name: nginx\n        image: nginx:1.27-alpine\n"
    )
    # Dry-run preview: returns the would-be object but persists nothing.
    preview = await connector.k8s_apply(op, target, {"manifest": manifest, "dry_run": "server"})
    assert preview["dry_run"] is True
    assert preview["applied"][0]["kind"] == "Deployment"

    api_client = await connector._get_api_client(target, op)
    apps_v1 = _client.AppsV1Api(api_client)
    from kubernetes_asyncio.client.exceptions import ApiException

    with pytest.raises(ApiException) as exc:
        await apps_v1.read_namespaced_deployment(name="nginx", namespace=ns_name)
    assert exc.value.status == 404, "dry-run must not have persisted the Deployment"

    # Real apply now creates it.
    applied = await connector.k8s_apply(op, target, {"manifest": manifest})
    assert applied["dry_run"] is False
    live = await apps_v1.read_namespaced_deployment(name="nginx", namespace=ns_name)
    assert live.metadata.name == "nginx"

    # Scale it and confirm before/after.
    scaled = await connector.k8s_scale(
        op, target, {"name": "nginx", "namespace": ns_name, "replicas": 3}
    )
    assert scaled["replicas_before"] == 1
    assert scaled["replicas_after"] == 3

    # Annotate + label, then read back off the live object.
    await connector.k8s_annotate(
        op,
        target,
        {
            "kind": "deployment",
            "name": "nginx",
            "namespace": ns_name,
            "annotations": {"meho.test/owner": "platform"},
        },
    )
    await connector.k8s_label(
        op,
        target,
        {"kind": "deployment", "name": "nginx", "namespace": ns_name, "labels": {"tier": "web"}},
    )
    refreshed = await apps_v1.read_namespaced_deployment(name="nginx", namespace=ns_name)
    assert refreshed.metadata.annotations.get("meho.test/owner") == "platform"
    assert refreshed.metadata.labels.get("tier") == "web"

    # Rollout restart stamps the pod-template annotation.
    restart = await connector.k8s_rollout_restart(
        op, target, {"name": "nginx", "namespace": ns_name}
    )
    rolled = await apps_v1.read_namespaced_deployment(name="nginx", namespace=ns_name)
    stamped = rolled.spec.template.metadata.annotations.get("kubectl.kubernetes.io/restartedAt")
    assert stamped == restart["restarted_at"]


@pytest.mark.asyncio
async def test_secret_create_redacts_values_against_k3s(
    write_namespace: Any,
) -> None:
    """A live Secret carries the value in-cluster but never in the broadcast.

    Positively asserts the secret material is ABSENT from the broadcast
    payload the publisher would ship (classify_op → credential_write →
    aggregate-only), per the #1403 acceptance criterion.
    """
    import base64

    from kubernetes_asyncio import client as _client

    from meho_backplane.broadcast.events import classify_op, redact_payload

    connector, target, op, ns_name = write_namespace
    raw_params = {
        "name": "db-creds",
        "namespace": ns_name,
        "string_data": {"password": "live-hunter2"},
    }
    summary = await connector.k8s_secret_create(op, target, dict(raw_params))
    # The handler response is value-free.
    assert "live-hunter2" not in str(summary)
    assert summary["data_keys"] == ["password"]

    # The value DID reach the cluster.
    api_client = await connector._get_api_client(target, op)
    core_v1 = _client.CoreV1Api(api_client)
    live = await core_v1.read_namespaced_secret(name="db-creds", namespace=ns_name)
    assert base64.b64decode(live.data["password"]).decode() == "live-hunter2"

    # The broadcast the publisher would emit is aggregate-only -- the
    # secret never reaches the SSE stream / Slack mirror.
    op_class = classify_op("k8s.secret.create")
    payload = redact_payload(op_class, {"params": raw_params}, "ok")
    assert "live-hunter2" not in str(payload)
    assert payload == {"op_class": "credential_write", "result_status": "ok"}


@pytest.mark.asyncio
async def test_job_create_then_delete_redacts_env_secret_against_k3s(
    write_namespace: Any,
) -> None:
    """A live Job's inline env secret never reaches the broadcast; delete reaps it."""
    from meho_backplane.broadcast.events import classify_op, redact_payload

    connector, target, op, ns_name = write_namespace
    job_params = {
        "name": "echo-job",
        "namespace": ns_name,
        "spec": {
            "backoffLimit": 0,
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "echo",
                            "image": "busybox:1.36",
                            "command": ["sh", "-c", "echo done"],
                            "env": [{"name": "TOKEN", "value": "job-topsecret"}],
                        }
                    ],
                }
            },
        },
    }
    created = await connector.k8s_job_create(op, target, dict(job_params))
    assert created == {"name": "echo-job", "namespace": ns_name, "created": True}
    assert "job-topsecret" not in str(created)

    # Broadcast redaction: aggregate-only, no env secret.
    payload = redact_payload(classify_op("k8s.job.create"), {"params": job_params}, "ok")
    assert "job-topsecret" not in str(payload)
    assert payload == {"op_class": "credential_write", "result_status": "ok"}

    # Delete the Job (Background cascade reaps its pods).
    deleted = await connector.k8s_delete(
        op,
        target,
        {
            "kind": "job",
            "name": "echo-job",
            "namespace": ns_name,
            "propagation_policy": "Background",
        },
    )
    assert deleted["deleted"] is True


@pytest.mark.asyncio
async def test_delete_rejects_namespace_kind_against_k3s(
    write_namespace: Any,
) -> None:
    """The v1 delete scope refuses namespace deletion even on a live cluster."""
    from meho_backplane.connectors.kubernetes.ops_write_dangerous import (
        UndeletableKindError,
    )

    connector, target, op, ns_name = write_namespace
    with pytest.raises(UndeletableKindError):
        await connector.k8s_delete(
            op,
            target,
            {"kind": "namespace", "name": ns_name, "namespace": ns_name},
        )


@pytest.mark.asyncio
async def test_cordon_uncordon_node_against_k3s(
    k3s_connector: tuple[KubernetesConnector, _K3sTarget],
) -> None:
    """Cordon then uncordon the single k3s node; reversible + eviction-free."""
    from kubernetes_asyncio import client as _client

    connector, target = k3s_connector
    op = _make_k3d_operator()
    api_client = await connector._get_api_client(target, op)
    core_v1 = _client.CoreV1Api(api_client)
    nodes = await core_v1.list_node()
    node_name = nodes.items[0].metadata.name
    try:
        cordoned = await connector.k8s_cordon(op, target, {"name": node_name})
        assert cordoned["unschedulable"] is True
        live = await core_v1.read_node(name=node_name)
        assert live.spec.unschedulable is True
    finally:
        uncordoned = await connector.k8s_cordon(op, target, {"name": node_name, "uncordon": True})
        assert uncordoned["unschedulable"] is False
