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


@pytest.fixture
async def k3s_connector(
    k3s_kubeconfig_and_target: tuple[dict[str, Any], _K3sTarget],
) -> AsyncIterator[tuple[KubernetesConnector, _K3sTarget]]:
    """Yield a connector whose loader returns the container's kubeconfig."""
    kubeconfig, target = k3s_kubeconfig_and_target

    async def _loader(_target: KubernetesTargetLike) -> dict[str, Any]:
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

    async def _loader(_target: KubernetesTargetLike) -> dict[str, Any]:
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
    await connector.fingerprint(target)
    cached_client_id_after_first = id(connector._api_clients[target.name])
    await connector.fingerprint(target)
    cached_client_id_after_second = id(connector._api_clients[target.name])
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
    result = await connector.k8s_namespace_list(target, {})
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
    result = await connector.k8s_node_list(target, {})
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
    result = await connector.k8s_ls(target, {"path": "/"})
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
    result = await connector.k8s_ls(target, {"path": "/kube-system"})
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
    """``k8s.ls /default/pods`` forwards to ``k8s.pod.list`` -- unknown_op until T3."""
    connector, target = k3s_connector
    result = await connector.k8s_ls(target, {"path": "/default/pods"})
    assert result["forwarded_to"] == "k8s.pod.list"
    inner = result["result"]
    # T3 hasn't shipped k8s.pod.list yet, so the dispatcher shim emits
    # the structured unknown_op envelope. When T3 lands, this test
    # gains a real-pod-row assertion in the inner result.
    assert inner["status"] == "error"
    assert inner["error"].startswith("unknown_op:")
