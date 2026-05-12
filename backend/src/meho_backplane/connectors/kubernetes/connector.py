# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""KubernetesConnector — fingerprint / probe / op-dispatch skeleton.

T1 ships the canary surface for the Kubernetes connector Initiative
(#320). Operations are filled in by T2-T5; this module only wires:

* Async kubeconfig load via an injectable :data:`KubeconfigLoader`.
* Per-target :class:`kubernetes_asyncio.client.ApiClient` cache,
  protected by a single :class:`asyncio.Lock` so concurrent first-use
  for the same target reads kubeconfig once.
* :meth:`fingerprint` against ``GET /version`` (``VersionApi.get_code``).
* :meth:`probe` as a kubeconfig-free TCP+TLS reachability check
  against ``/readyz`` — explicitly does **not** use the operator's
  kubeconfig so an auth misconfiguration never surfaces as a probe
  failure.
* :meth:`execute` returning a structured ``unknown_op`` error for
  every op_id (the per-op handlers land in T2+).
* :meth:`aclose` releasing every cached :class:`ApiClient` — wired
  from the FastAPI lifespan teardown by the registry once G0.2-T2
  (#241) lands.

Product flavour (``"rke2"`` / ``"k3s"`` / ``"eks"`` / ``"gke"`` /
``"aks"`` / ``"vanilla"``) is derived from the ``gitVersion`` suffix
returned by the API server — sufficient for v0.2's version-tagged
doc/kb lookup and broadcast classifier without an extra round-trip.
"""

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from kubernetes_asyncio import client, config

from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.kubernetes.kubeconfig import (
    KubeconfigLoader,
    KubernetesTargetLike,
    load_kubeconfig_from_vault,
)
from meho_backplane.connectors.schemas import (
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

__all__ = ["KubernetesConnector", "product_from_git_version"]

_log = structlog.get_logger(__name__)

_DEFAULT_K8S_PORT = 6443
_PROBE_TIMEOUT_SECONDS = 5.0
_PROBE_OK_STATUSES = frozenset({200, 401})


def product_from_git_version(git_version: str) -> str:
    """Map a Kubernetes ``gitVersion`` string to a product slug.

    The k8s API server's ``/version`` endpoint returns ``gitVersion`` in
    the form ``v<major>.<minor>.<patch><suffix>``. The suffix encodes the
    distribution: ``+rke2r1`` for RKE2, ``+k3s1`` for K3s, ``-eks-…`` for
    EKS, ``-gke.…`` for GKE, ``-aks`` for AKS. Vanilla upstream has no
    suffix (or ``+0`` for some custom builds).
    """
    if "+rke2" in git_version:
        return "rke2"
    if "+k3s" in git_version:
        return "k3s"
    if "-eks-" in git_version:
        return "eks"
    if "-gke." in git_version:
        return "gke"
    if "-aks" in git_version:
        return "aks"
    return "vanilla"


class KubernetesConnector(Connector):
    """Kubernetes connector — reads kubeconfig per target, caches the client."""

    product = "kubernetes"

    def __init__(
        self,
        *,
        kubeconfig_loader: KubeconfigLoader | None = None,
    ) -> None:
        self._kubeconfig_loader: KubeconfigLoader = (
            kubeconfig_loader if kubeconfig_loader is not None else load_kubeconfig_from_vault
        )
        self._api_clients: dict[str, client.ApiClient] = {}
        self._lock = asyncio.Lock()

    async def fingerprint(self, target: KubernetesTargetLike) -> FingerprintResult:
        """Canonical fingerprint built from ``VersionApi.get_code()``."""
        api_client = await self._get_api_client(target)
        version_api = client.VersionApi(api_client)
        version = await version_api.get_code()
        return FingerprintResult(
            vendor="kubernetes",
            product=product_from_git_version(version.git_version),
            version=version.git_version,
            build=version.build_date,
            edition=None,
            reachable=True,
            probed_at=datetime.now(UTC),
            probe_method="GET /version",
            extras={
                "major": version.major,
                "minor": version.minor,
                "platform": version.platform,
                "go_version": version.go_version,
                "git_commit": version.git_commit,
                "git_tree_state": version.git_tree_state,
            },
        )

    async def probe(self, target: KubernetesTargetLike) -> ProbeResult:
        """Kubeconfig-free reachability check against ``/readyz`` (or ``/healthz``).

        TLS verification is intentionally disabled (NOSONAR S4830): the
        probe is a reachability signal, not an auth check, and runs
        before any kubeconfig is loaded, so the CA bundle is not yet
        known. A 401 response is treated as success — it means the API
        server is up and speaking TLS; auth surfaces at :meth:`execute`
        time. Real certificate validation happens via the kubeconfig's
        ``certificate-authority-data`` once the operator's identity is
        in play.

        Endpoint fallback: ``GET /readyz`` first; on HTTP 404 retry
        ``GET /healthz`` (legacy clusters that predate ``/readyz`` or
        have it disabled). The first response whose status is in
        :data:`_PROBE_OK_STATUSES` short-circuits the probe.
        """
        port = target.port if target.port is not None else _DEFAULT_K8S_PORT
        base_url = f"https://{target.host}:{port}"
        start = time.monotonic()
        probed_at = datetime.now(UTC)
        endpoint = "/readyz"
        try:
            async with httpx.AsyncClient(
                verify=False,  # NOSONAR S4830 — kubeconfig-free reachability probe; see docstring
                timeout=_PROBE_TIMEOUT_SECONDS,
            ) as http:
                resp = await http.get(f"{base_url}{endpoint}")
                if resp.status_code == 404:
                    endpoint = "/healthz"
                    resp = await http.get(f"{base_url}{endpoint}")
        except (httpx.HTTPError, OSError) as exc:
            return ProbeResult(
                ok=False,
                reason=f"{type(exc).__name__}: {exc}",
                latency_ms=None,
                probed_at=probed_at,
            )
        latency_ms = (time.monotonic() - start) * 1000.0
        if resp.status_code in _PROBE_OK_STATUSES:
            return ProbeResult(ok=True, latency_ms=latency_ms, probed_at=probed_at)
        return ProbeResult(
            ok=False,
            reason=f"HTTP {resp.status_code} on {endpoint}",
            latency_ms=latency_ms,
            probed_at=probed_at,
        )

    async def execute(
        self,
        target: KubernetesTargetLike,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Skeleton dispatcher — every op_id returns a structured ``unknown_op``.

        T2-T5 of #320 fill in the per-op handlers via a per-product
        ``_op_map``. Until then the canonical error shape is in place so
        callers can distinguish "connector exists but op unwired" from
        "no connector for product".
        """
        return OperationResult(
            status="error",
            op_id=op_id,
            error=f"unknown_op: {op_id}",
            duration_ms=0.0,
            extras={"known_ops": []},
        )

    async def aclose(self) -> None:
        """Close every cached :class:`ApiClient`. Idempotent."""
        async with self._lock:
            for api_client in self._api_clients.values():
                await api_client.close()
            self._api_clients.clear()

    @staticmethod
    def _cache_key(target: KubernetesTargetLike) -> str:
        """Globally unique cache key for *target*.

        Keyed on ``secret_ref`` (the Vault path the kubeconfig lives
        at) rather than ``target.name``. Once G0.3 (#224) lands its
        ``Target`` model, target names are unique only within a tenant
        — two tenants legitimately holding a target both named
        ``"rke2-meho"`` would otherwise share an :class:`ApiClient`
        built from whichever kubeconfig loaded first, and the second
        tenant's ops would silently execute against the first
        tenant's cluster. The Vault path is the operator's chosen
        opaque identifier for the kubeconfig and is globally unique
        by the consumer's ``targets.yaml`` convention. Swap to
        ``target.id`` when G0.3 finalises a row-PK shape.
        """
        return target.secret_ref

    async def _get_api_client(self, target: KubernetesTargetLike) -> client.ApiClient:
        """Resolve (and cache) the :class:`ApiClient` for *target*.

        The single lock serialises concurrent first-use for any target;
        in practice the second caller hits the cache fast-path. The
        slow kubeconfig read happens under the lock so two concurrent
        callers for the same target don't both pay the cost.
        """
        key = self._cache_key(target)
        async with self._lock:
            cached = self._api_clients.get(key)
            if cached is not None:
                return cached
            kubeconfig_dict = await self._kubeconfig_loader(target)
            api_client = await config.new_client_from_config_dict(kubeconfig_dict)
            self._api_clients[key] = api_client
            _log.info(
                "kubernetes_api_client_built",
                target=target.name,
                host=target.host,
            )
            return api_client
