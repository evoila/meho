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
            secret_ref="kv/data/k8s/k3s-test",
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
