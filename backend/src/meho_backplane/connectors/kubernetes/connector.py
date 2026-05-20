# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""KubernetesConnector -- fingerprint / probe / dispatcher-shim.

The G3.2-T1 (#321) skeleton wired ``fingerprint`` + ``probe`` + a stub
``execute`` that returned ``unknown_op`` for every op_id. The G0.6
refactor (#391) keeps the fingerprint + probe paths byte-for-byte
unchanged and refactors ``execute`` into a thin shim that delegates to
the G0.6 dispatcher substrate:

* :attr:`KubernetesConnector.version` /
  :attr:`KubernetesConnector.impl_id` advertise the registry v2 key
  ``("k8s", "1.x", "kubernetes-asyncio")``. The shipped v1 entry
  (``register_connector("k8s", ...)``) is retained for
  ``get_connector("k8s")`` callers (resolver tests, ``/api/v1/health``
  Vault federation probe shape). The chassis dispatch route that
  originally motivated it (``POST /api/v1/connectors/{product}/{op_id}``)
  was deprecated and removed by G0.6-T11 (#412); the canonical
  dispatch surface is now ``POST /api/v1/operations/call``.
* :meth:`register_operations` is a classmethod called from the
  application lifespan. It walks
  :data:`~meho_backplane.connectors.kubernetes.ops.KUBERNETES_OPS` and
  upserts each row into ``endpoint_descriptor`` via
  :func:`~meho_backplane.operations.typed_register.register_typed_operation`.
  Idempotent (the helper's body-hash skip-re-embed branch keeps pod
  restarts cheap).
* :meth:`about` is the canary op the refactor registers against the
  new substrate. The handler reuses the same
  ``kubernetes_asyncio.client.VersionApi.get_code`` call
  :meth:`fingerprint` already issues, returning a flat dict the
  dispatcher's reducer wraps into ``OperationResult.result``.
* :meth:`execute` shims into the dispatcher's lookup + handler-resolve
  + invoke path so unknown op_ids return the same
  ``OperationResult(status="error", error="unknown_op: ...")`` shape
  the dispatcher emits everywhere else. The operator-aware path is
  ``call_operation`` / ``/api/v1/operations/call`` via the G0.6
  meta-tools; :meth:`execute` remains the typed-connector entry the
  dispatcher invokes for ``source_kind == "typed"`` rows.

The skeleton's per-target :class:`kubernetes_asyncio.client.ApiClient`
cache, the asyncio-lock protecting it, and :meth:`aclose` are all
preserved verbatim.

Product flavour (``"rke2"`` / ``"k3s"`` / ``"eks"`` / ``"gke"`` /
``"aks"`` / ``"vanilla"``) is derived from the ``gitVersion`` suffix
returned by the API server -- sufficient for v0.2's version-tagged
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
from meho_backplane.connectors.kubernetes.ops import KUBERNETES_OPS
from meho_backplane.connectors.kubernetes.ops_core import (
    K8S_CLUSTER_KINDS,
    K8S_NAMESPACED_KIND_LISTERS,
    namespace_row,
    node_row,
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


#: Plural kind labels (kubectl-style) -> singular op_id segment. Only
#: the irregulars need an entry; everything else (``pods``, ``services``,
#: ``configmaps``, ``nodes``) takes the strip-trailing-s default branch
#: in :func:`_normalise_kind_to_singular`.
_PLURAL_TO_SINGULAR_KIND: dict[str, str] = {
    "ingresses": "ingress",
    "persistentvolumeclaims": "persistentvolumeclaim",
    "persistentvolumes": "persistentvolume",
    "storageclasses": "storageclass",
}


def _normalise_kind_to_singular(kind: str) -> str:
    """Map a ``kubectl get <kind>``-style plural to the singular op-id segment.

    The path the operator types is plural-shaped (``/argocd/pods``,
    matching their ``kubectl`` muscle memory); the op_id namespace under
    #320 is singular-shaped (``k8s.pod.list``). The mapping is local to
    the connector because the rest of the substrate doesn't care --
    op_ids are opaque dotted strings to the dispatcher.

    Three branches, in order:

    1. Explicit plural in :data:`_PLURAL_TO_SINGULAR_KIND` -- the
       irregulars (``ingresses``, ``persistentvolumeclaims``) that
       don't strip a trailing ``s`` cleanly.
    2. An operator-typed singular that already matches one of the
       mapped singular forms (``ingress``, ``storageclass``,
       ``persistentvolume``, ``persistentvolumeclaim``). Return as-is
       to avoid the strip-trailing-s branch mangling ``ingress`` into
       ``ingres``.
    3. Default: strip a trailing ``s`` (``pods`` -> ``pod``,
       ``services`` -> ``service``).
    """
    if kind in _PLURAL_TO_SINGULAR_KIND:
        return _PLURAL_TO_SINGULAR_KIND[kind]
    if kind in _PLURAL_TO_SINGULAR_KIND.values():
        return kind
    if kind.endswith("s") and len(kind) > 1:
        return kind[:-1]
    return kind


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
    """Kubernetes connector -- reads kubeconfig per target, caches the client."""

    # Registry v2 metadata (G0.6-T3 #394 + #391 refactor). The product
    # slug ``"k8s"`` is the v2 canonical form aligned with the
    # ``connector_id="k8s-1.x"`` shape the dispatcher's parser produces
    # (:func:`~meho_backplane.operations._lookup.parse_connector_id`).
    # The v1 entry registered in :mod:`__init__` uses ``"kubernetes"``
    # for chassis-route backward compat -- both keys resolve to the
    # same connector class via the registry's two-layer storage.
    product = "k8s"
    version = "1.x"
    impl_id = "kubernetes-asyncio"

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

    async def about(
        self,
        target: KubernetesTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Return product flavour + version snapshot for *target*.

        Op-id: ``k8s.about``. The dispatcher routes here after the JSON
        Schema validator has accepted ``params`` (declared empty in
        :data:`~meho_backplane.connectors.kubernetes.ops.KUBERNETES_OPS`)
        and the reducer wraps the returned dict into
        ``OperationResult.result``. The handler reuses the same
        :meth:`kubernetes_asyncio.client.VersionApi.get_code` call
        :meth:`fingerprint` issues so the cluster pays one round-trip per
        ``k8s.about`` dispatch regardless of how many other ops touch
        ``VersionApi``.

        The returned dict is intentionally flat -- no nested
        ``extras`` -- because the dispatcher's
        :class:`~meho_backplane.operations.reducer.PassThroughReducer`
        forwards the value verbatim. Future reducers (real JSONFlux
        reduction in a follow-on Initiative) flatten nested shapes
        anyway; staying flat now means the v0.2 callers see the same
        keys before and after the reducer swap.
        """
        del params  # declared in schema; the handler intentionally ignores them
        api_client = await self._get_api_client(target)
        version_api = client.VersionApi(api_client)
        version = await version_api.get_code()
        return {
            "product": product_from_git_version(version.git_version),
            "git_version": version.git_version,
            "build_date": version.build_date,
            "major": version.major,
            "minor": version.minor,
            "platform": version.platform,
            "go_version": version.go_version,
            "git_commit": version.git_commit,
            "git_tree_state": version.git_tree_state,
        }

    async def k8s_namespace_list(
        self,
        target: KubernetesTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """List namespaces -- one row per namespace with phase / age / labels.

        Op-id: ``k8s.namespace.list``. Wraps ``CoreV1Api.list_namespace()``
        and projects each :class:`V1Namespace` through
        :func:`~meho_backplane.connectors.kubernetes.ops_core.namespace_row`
        so the wire shape is stable across the test seam (where
        ``namespace_row`` is unit-tested directly against synthetic
        :class:`V1Namespace` instances) and the live API path.

        ``params`` is declared empty in the op's
        :attr:`~meho_backplane.connectors.kubernetes.ops_core.K8S_NAMESPACE_LIST_PARAMETER_SCHEMA`;
        the dispatcher's :class:`Draft202012Validator` rejects any extra
        keys before this handler runs.
        """
        del params
        api_client = await self._get_api_client(target)
        core_v1 = client.CoreV1Api(api_client)
        resp = await core_v1.list_namespace()
        rows = [namespace_row(ns) for ns in resp.items]
        return {"rows": rows, "total": len(rows)}

    async def k8s_node_list(
        self,
        target: KubernetesTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """List cluster nodes -- name / status / roles / version / kernel / IP / taints.

        Op-id: ``k8s.node.list``. Wraps ``CoreV1Api.list_node()`` and
        projects each :class:`V1Node` through
        :func:`~meho_backplane.connectors.kubernetes.ops_core.node_row`.
        The Ready-condition mapping ("Ready" / "NotReady" / "Unknown") and
        the role-label derivation are both helpers in :mod:`ops_core` so
        the unit tests pin those mappings against synthetic
        :class:`V1Node` instances without an event loop.
        """
        del params
        api_client = await self._get_api_client(target)
        core_v1 = client.CoreV1Api(api_client)
        resp = await core_v1.list_node()
        rows = [node_row(n) for n in resp.items]
        return {"rows": rows, "total": len(rows)}

    async def k8s_service_list(
        self,
        target: KubernetesTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """List services in a namespace -- name / type / cluster_ip / ports / selector.

        Op-id: ``k8s.service.list``. Wraps
        ``CoreV1Api.list_namespaced_service(namespace)`` and projects
        each :class:`V1Service` through
        :func:`~meho_backplane.connectors.kubernetes.ops_network.service_row`.
        The helper is pure so the unit suite pins the wire shape against
        synthetic fixtures without booting an event loop.
        """
        from meho_backplane.connectors.kubernetes.ops_network import service_row

        api_client = await self._get_api_client(target)
        core_v1 = client.CoreV1Api(api_client)
        resp = await core_v1.list_namespaced_service(namespace=params["namespace"])
        rows = [service_row(s) for s in resp.items]
        return {"rows": rows, "total": len(rows)}

    async def k8s_ingress_list(
        self,
        target: KubernetesTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """List ingresses in a namespace -- class / hosts / TLS hosts / rules.

        Op-id: ``k8s.ingress.list``. Wraps
        ``NetworkingV1Api.list_namespaced_ingress(namespace)`` and
        projects each :class:`V1Ingress` through
        :func:`~meho_backplane.connectors.kubernetes.ops_network.ingress_row`.
        """
        from meho_backplane.connectors.kubernetes.ops_network import ingress_row

        api_client = await self._get_api_client(target)
        networking_v1 = client.NetworkingV1Api(api_client)
        resp = await networking_v1.list_namespaced_ingress(namespace=params["namespace"])
        rows = [ingress_row(i) for i in resp.items]
        return {"rows": rows, "total": len(rows)}

    async def k8s_configmap_list(
        self,
        target: KubernetesTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """List configmaps in a namespace -- **keys only, no values**.

        Op-id: ``k8s.configmap.list``. Wraps
        ``CoreV1Api.list_namespaced_config_map(namespace)`` and projects
        each :class:`V1ConfigMap` through
        :func:`~meho_backplane.connectors.kubernetes.ops_config.configmap_list_row`,
        which deliberately omits ``data`` / ``binary_data`` values.
        Operators wanting values call ``k8s.configmap.info`` per
        configmap so the audit row records the targeted read.
        """
        from meho_backplane.connectors.kubernetes.ops_config import configmap_list_row

        api_client = await self._get_api_client(target)
        core_v1 = client.CoreV1Api(api_client)
        resp = await core_v1.list_namespaced_config_map(namespace=params["namespace"])
        rows = [configmap_list_row(cm) for cm in resp.items]
        return {"rows": rows, "total": len(rows)}

    async def k8s_configmap_info(
        self,
        target: KubernetesTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Read a single configmap with full data + binary_data.

        Op-id: ``k8s.configmap.info``. Wraps
        ``CoreV1Api.read_namespaced_config_map(name, namespace)`` and
        projects the result through
        :func:`~meho_backplane.connectors.kubernetes.ops_config.configmap_info`.
        Counterpart to ``k8s.configmap.list``; the targeted read records
        a per-configmap audit row.
        """
        from meho_backplane.connectors.kubernetes.ops_config import configmap_info

        api_client = await self._get_api_client(target)
        core_v1 = client.CoreV1Api(api_client)
        cm = await core_v1.read_namespaced_config_map(
            name=params["name"], namespace=params["namespace"]
        )
        return configmap_info(cm)

    async def k8s_event_list(
        self,
        target: KubernetesTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """List events in a namespace, sorted most-recent-first, truncated to ``limit``.

        Op-id: ``k8s.event.list``. Wraps
        ``CoreV1Api.list_namespaced_event(namespace, field_selector=..., limit=...)``
        and projects each :class:`CoreV1Event` through
        :func:`~meho_backplane.connectors.kubernetes.ops_events.event_row`.

        The K8s API server returns events in resource-version order
        (creation order), **not** last-seen order, and offers no
        server-side ``orderBy`` knob -- see
        https://kubernetes.io/docs/reference/using-api/api-concepts/#resource-versions
        for the ordering contract. Passing the caller's ``limit``
        directly to the API would therefore truncate an arbitrary
        creation-ordered prefix before the client-side
        :func:`sort_event_rows_recent_first` ever runs, defeating the
        "N most-recent" acceptance criterion: an event created hours
        ago but firing every minute would drop out of a small
        ``--limit 10`` result while an old quiescent event near the
        top of the creation order kept its slot.

        Instead, the handler always asks the API for up to
        :data:`MAX_EVENT_LIMIT` rows -- a bounded superset -- then
        sorts the projection by ``last_seen_seconds`` and truncates
        client-side to the caller's ``limit``. The cap is set in
        :mod:`ops_events` (currently 500) at the same operator-ergonomic
        ceiling the schema's ``maximum`` enforces; above that the
        operator is better served by a ``field_selector`` than by a
        bigger result set (the K8s API itself paginates above ~1k
        items per response in most deployments).
        """
        from meho_backplane.connectors.kubernetes.ops_events import (
            DEFAULT_EVENT_LIMIT,
            MAX_EVENT_LIMIT,
            event_row,
            sort_event_rows_recent_first,
        )

        namespace = params["namespace"]
        limit = int(params.get("limit", DEFAULT_EVENT_LIMIT))
        # Defence in depth: the schema's ``maximum`` already enforces
        # the cap. Keep the explicit clamp so a future schema relaxation
        # cannot exceed the ceiling silently -- same discipline as
        # ``k8s.logs``'s tail clamp.
        if limit > MAX_EVENT_LIMIT:
            limit = MAX_EVENT_LIMIT
        field_selector = params.get("field_selector")

        api_client = await self._get_api_client(target)
        core_v1 = client.CoreV1Api(api_client)
        # Always pull up to MAX_EVENT_LIMIT rows so the client-side
        # sort sees the full recency-relevant superset; the caller's
        # ``limit`` truncates after the sort. See the docstring above
        # for the ordering rationale.
        kwargs: dict[str, Any] = {"namespace": namespace, "limit": MAX_EVENT_LIMIT}
        if field_selector:
            kwargs["field_selector"] = field_selector
        resp = await core_v1.list_namespaced_event(**kwargs)
        rows = [event_row(e) for e in resp.items]
        sorted_rows = sort_event_rows_recent_first(rows)
        truncated = sorted_rows[:limit]
        return {"rows": truncated, "total": len(truncated)}

    async def k8s_ls(
        self,
        target: KubernetesTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Synthetic walker -- list inventory at a logical path.

        Op-id: ``k8s.ls``. Three shapes by *path*:

        * ``/`` (or omitted) -- ``{namespaces: [...], cluster_kinds: [...]}``.
          ``namespaces`` is the sorted namespace names; ``cluster_kinds`` is
          the fixed list from
          :data:`~meho_backplane.connectors.kubernetes.ops_core.K8S_CLUSTER_KINDS`.
        * ``/<namespace>`` -- ``{namespace: <ns>, kinds: [{kind, count}],
          cluster_kinds_omitted: true}``. The handler probes each kind in
          :data:`~meho_backplane.connectors.kubernetes.ops_core.K8S_NAMESPACED_KIND_LISTERS`
          with ``limit=1`` and reads the response's
          ``metadata.remaining_item_count`` to derive the count without
          pulling every row -- the operator-facing "how many pods are in
          argocd?" question costs one round-trip per kind, not one per
          row.
        * ``/<namespace>/<kind>`` -- forwards to ``k8s.<kind>.list`` via
          :meth:`execute` (the dispatcher shim). Kinds whose ``list`` op
          hasn't shipped yet come back through ``execute``'s structured
          ``unknown_op`` envelope; the handler does not pretend to know
          which kinds will eventually be registered.

        Forwarding-through-execute (rather than calling
        ``CoreV1Api.list_namespaced_<kind>`` directly here) means the
        kind-specific ops' own dispatcher path -- parameter validation,
        future reducer-driven handle creation, audit row, broadcast --
        runs verbatim. The forwarding handler is structural plumbing, not
        a semantic shortcut.
        """
        raw_path = params.get("path", "/")
        # Normalise the path: empty string and "/" both map to the root;
        # leading slash is stripped before splitting so "/argocd" parses
        # to ["argocd"], not ["", "argocd"].
        if not isinstance(raw_path, str):
            # Defensive: the JSON Schema's ``type: string`` should catch
            # non-string inputs before the handler runs, but the schema's
            # ``default`` clause means an absent param arrives as the
            # schema default ("/"). A wrong-type override surfaces here.
            raise TypeError(f"k8s.ls path must be a string, got {type(raw_path).__name__}")
        segments = [s for s in raw_path.split("/") if s]

        if not segments:
            return await self._k8s_ls_root(target)
        if len(segments) == 1:
            return await self._k8s_ls_namespace(target, segments[0])
        if len(segments) == 2:
            return await self._k8s_ls_namespace_kind(target, segments[0], segments[1])
        # Deeper paths aren't a documented v0.2 shape; collapse to the
        # namespace/kind forwarder against the first two segments. The
        # operator gets a useful result rather than an opaque error.
        return await self._k8s_ls_namespace_kind(target, segments[0], segments[1])

    async def _k8s_ls_root(
        self,
        target: KubernetesTargetLike,
    ) -> dict[str, Any]:
        """Cluster-root view: list namespace names + the fixed cluster-kind list."""
        api_client = await self._get_api_client(target)
        core_v1 = client.CoreV1Api(api_client)
        resp = await core_v1.list_namespace()
        names = sorted(
            ns.metadata.name
            for ns in resp.items
            if ns.metadata is not None and ns.metadata.name is not None
        )
        return {
            "path": "/",
            "namespaces": names,
            "cluster_kinds": list(K8S_CLUSTER_KINDS),
        }

    async def _k8s_ls_namespace(
        self,
        target: KubernetesTargetLike,
        namespace: str,
    ) -> dict[str, Any]:
        """Per-namespace kind summary -- one round-trip per probed kind.

        Each probed kind is fetched with ``limit=1`` so the server can
        emit the row count via :class:`V1ListMeta.remaining_item_count`
        without streaming every row back. The total count is
        ``len(items) + (remaining_item_count or 0)`` because the server
        treats the first ``limit`` rows as inline and the remainder as
        the "still to fetch" tail.

        A per-kind probe that raises (RBAC-shaped 403, deprecated kind on
        an older cluster, transient API server blip) lands as
        ``count=None`` with the exception class on ``error`` so the
        operator sees which kinds aren't enumerable rather than a single
        500-shaped failure for the whole ls.
        """
        api_client = await self._get_api_client(target)
        core_v1 = client.CoreV1Api(api_client)
        kinds: list[dict[str, Any]] = []
        for kind_label, method_name in K8S_NAMESPACED_KIND_LISTERS:
            method = getattr(core_v1, method_name, None)
            if method is None:
                # Defensive: a typo in the table would otherwise surface as
                # an opaque AttributeError mid-walk. Record it as the
                # error shape so the rest of the kinds still report.
                kinds.append(
                    {
                        "kind": kind_label,
                        "count": None,
                        "error": f"AttributeError: CoreV1Api has no {method_name!r}",
                    }
                )
                continue
            try:
                resp = await method(namespace=namespace, limit=1)
                inline = len(resp.items) if resp.items is not None else 0
                remaining = 0
                if resp.metadata is not None and resp.metadata.remaining_item_count is not None:
                    remaining = int(resp.metadata.remaining_item_count)
                kinds.append({"kind": kind_label, "count": inline + remaining})
            except Exception as exc:
                kinds.append(
                    {
                        "kind": kind_label,
                        "count": None,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        return {
            "path": f"/{namespace}",
            "namespace": namespace,
            "kinds": kinds,
            "cluster_kinds_omitted": True,
        }

    async def _k8s_ls_namespace_kind(
        self,
        target: KubernetesTargetLike,
        namespace: str,
        kind: str,
    ) -> dict[str, Any]:
        """Forward to ``k8s.<kind>.list`` via the connector's dispatcher shim.

        The shim resolves the kind-specific op's descriptor; an unknown
        kind (T3/T4 hasn't shipped its ``list`` op yet) comes back as the
        structured ``unknown_op`` envelope and the forwarder propagates
        it verbatim in the ``result`` field, so the operator sees the
        sub-op's exact error shape rather than a translated one.

        ``kind`` is normalised before dispatch: operators type the
        plural form (``pods``, matching ``kubectl get pods``), but the
        op-id namespace follows the singular convention from #320's op
        listing (``k8s.pod.list``, not ``k8s.pods.list``). The
        :data:`_PLURAL_TO_SINGULAR_KIND` map handles the common irregulars
        that don't strip a trailing ``s`` cleanly (``ingresses`` ->
        ``ingress``, ``persistentvolumeclaims`` ->
        ``persistentvolumeclaim``); everything else (``pods`` ->
        ``pod``, ``services`` -> ``service``) takes the strip-trailing-s
        branch. Operators who pass a singular form directly
        (``/argocd/pod``) get a no-op normalisation.
        """
        op_kind = _normalise_kind_to_singular(kind)
        sub_op_id = f"k8s.{op_kind}.list"
        sub_params = {"namespace": namespace}
        sub_result = await self.execute(target, sub_op_id, sub_params)
        # OperationResult is a Pydantic model; ``.model_dump()`` gives
        # the dict shape future dispatch-recording machinery (audit row,
        # broadcast event) can serialise without an extra round-trip.
        return {
            "path": f"/{namespace}/{kind}",
            "forwarded_to": sub_op_id,
            "result": sub_result.model_dump(mode="json"),
        }

    async def logs(
        self,
        target: KubernetesTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for the ``k8s.logs`` op.

        Op-id: ``k8s.logs``. Delegates to
        :func:`~meho_backplane.connectors.kubernetes.ops_logs.k8s_logs`
        with ``self`` injected as the ``connector`` argument so the
        handler can reach the cached :class:`ApiClient` via
        :meth:`_get_api_client` without an extra registry lookup. The
        module-level function carries the real logic so a future
        per-op-handler-file split (one ``ops_<verb>.py`` per op,
        without a class) keeps the API stable.

        Schema validation runs in the dispatcher before this method
        is called; the function-level handler re-reads only
        validated values.
        """
        from meho_backplane.connectors.kubernetes.ops_logs import k8s_logs

        return await k8s_logs(self, target, params)

    async def k8s_pod_list(
        self,
        target: KubernetesTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for the ``k8s.pod.list`` op (G3.2-T3 #323).

        Delegates to
        :func:`~meho_backplane.connectors.kubernetes.ops_workload.k8s_pod_list`.
        Same module-level-function shape :meth:`logs` uses so a future
        per-op-handler-file split keeps the registration API stable.
        """
        from meho_backplane.connectors.kubernetes.ops_workload import (
            k8s_pod_list as _k8s_pod_list,
        )

        return await _k8s_pod_list(self, target, params)

    async def k8s_pod_info(
        self,
        target: KubernetesTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for the ``k8s.pod.info`` op (G3.2-T3 #323)."""
        from meho_backplane.connectors.kubernetes.ops_workload import (
            k8s_pod_info as _k8s_pod_info,
        )

        return await _k8s_pod_info(self, target, params)

    async def k8s_deployment_list(
        self,
        target: KubernetesTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for the ``k8s.deployment.list`` op (G3.2-T3 #323)."""
        from meho_backplane.connectors.kubernetes.ops_workload import (
            k8s_deployment_list as _k8s_deployment_list,
        )

        return await _k8s_deployment_list(self, target, params)

    async def k8s_deployment_info(
        self,
        target: KubernetesTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for the ``k8s.deployment.info`` op (G3.2-T3 #323)."""
        from meho_backplane.connectors.kubernetes.ops_workload import (
            k8s_deployment_info as _k8s_deployment_info,
        )

        return await _k8s_deployment_info(self, target, params)

    @classmethod
    async def register_operations(cls) -> None:
        """Upsert every op in :data:`KUBERNETES_OPS` into ``endpoint_descriptor``.

        Called from the application lifespan after the registry has
        eager-imported every connector module. Walks
        :data:`~meho_backplane.connectors.kubernetes.ops.KUBERNETES_OPS`
        and routes each row through
        :func:`~meho_backplane.operations.typed_register.register_typed_operation`,
        which:

        * Derives ``handler_ref`` from the bound method's
          ``__module__`` + ``__qualname__`` (e.g.
          ``"meho_backplane.connectors.kubernetes.connector.KubernetesConnector.about"``).
        * Inserts a new row on first call; skips the embedding compute
          on re-call with unchanged summary / description / tags
          (body-hash skip-re-embed branch).
        * Always advances ``updated_at`` so operators can grep the
          last-registration timestamp.

        Idempotent across pod restarts. Errors propagate to the
        lifespan; the fail-fast deployment shape the rest of the
        chassis tasks established is what the operator wants here
        (a missing migration or a partial DB state is a deploy bug,
        not a runtime degradation).
        """
        # Imported lazily so a test that imports the connector module
        # without the operations package available (e.g. an isolated
        # unit test of ``product_from_git_version``) still works -- the
        # operations package transitively imports the embedding service,
        # which pulls in ONNX runtime and a 100 MB+ model on first
        # touch.
        from meho_backplane.operations.typed_register import register_typed_operation

        # Bind handler attrs once into a list of (op, bound-method)
        # tuples so the error message names the op_id when a typo in
        # KUBERNETES_OPS' handler_attr would otherwise surface as a
        # confusing ``AttributeError`` deep inside the helper.
        bindings: list[tuple[Any, Any]] = []
        for op in KUBERNETES_OPS:
            handler = getattr(cls, op.handler_attr, None)
            if handler is None:
                raise AttributeError(
                    f"KubernetesConnector op {op.op_id!r} declares "
                    f"handler_attr={op.handler_attr!r} but the class has no such attribute"
                )
            bindings.append((op, handler))

        for op, handler in bindings:
            await register_typed_operation(
                product=cls.product,
                version=cls.version,
                impl_id=cls.impl_id,
                op_id=op.op_id,
                handler=handler,
                summary=op.summary,
                description=op.description,
                parameter_schema=op.parameter_schema,
                response_schema=op.response_schema,
                group_key=op.group_key,
                # G0.9-T4a #731 placeholder paired with ``group_key``;
                # T4b #732 replaces with a curated blurb per group
                # (``cluster`` / ``core`` / ``workloads`` / ``network`` /
                # ``config`` / ``events`` / ``logs``). When the op has
                # no ``group_key`` (``None``), pass ``None`` so the
                # pairing validator stays happy.
                when_to_use=("TODO: curate (T4b #732)" if op.group_key is not None else None),
                tags=list(op.tags),
                safety_level=op.safety_level,
                requires_approval=op.requires_approval,
                llm_instructions=op.llm_instructions,
            )
        _log.info(
            "kubernetes_operations_registered",
            count=len(bindings),
            product=cls.product,
            version=cls.version,
            impl_id=cls.impl_id,
        )

    # code-quality-allow: pre-existing G3.2-T1 #321 skeleton; T11 #412
    # only edits the docstring (function is shorter post-edit). Refactor
    # into helpers is deferred to a separate Task.
    async def execute(
        self,
        target: KubernetesTargetLike,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Dispatcher shim -- delegates to G0.6's ``dispatch``-shaped lookup.

        Routes for *op_id* by:

        1. Looking up the descriptor for
           ``(product=cls.product, version=cls.version, impl_id=cls.impl_id, op_id)``
           against the global / built-in row set
           (``tenant_id IS NULL`` -- typed registrations are always
           global by construction).
        2. Unknown op_id -> the structured ``unknown_op``
           :class:`OperationResult` the dispatcher itself produces, via
           :func:`~meho_backplane.operations._errors.result_unknown_op`.
        3. Known op_id -> resolves ``descriptor.handler_ref`` via
           :func:`~meho_backplane.operations._handler_resolve.import_handler`,
           binds it against this instance when the resolved symbol is
           an unbound method (the bound-method case is the typed-
           connector convention), and invokes it with
           ``(target, params)``.

        The shim is intentionally **operator-less** -- direct callers
        (typed-connector internals, composite handlers) don't carry an
        :class:`~meho_backplane.auth.operator.Operator`, so the full
        dispatcher path (policy gate, audit, broadcast) doesn't run
        from this entry point. The operator-aware surface is
        ``POST /api/v1/operations/call`` via the G0.6 meta-tools; the
        pre-G0.6 chassis route was removed by G0.6-T11 (#412). Within
        the operator-less constraint the shim's contract is:

        * Same ``unknown_op`` shape the dispatcher emits.
        * Same ``connector_error`` shape on handler exceptions.
        * Same ``invalid_params`` shape on schema-validation failures.

        Result envelope mirrors :class:`OperationResult` so the
        FastAPI route's ``unknown_op``-extraction logic continues to
        work unchanged.
        """
        # Lazy imports for the same rationale documented on
        # ``register_operations`` -- pure-python tests that exercise
        # ``fingerprint``/``probe`` shouldn't pay the operations
        # package's import cost.
        from sqlalchemy import select

        from meho_backplane.db.engine import get_sessionmaker
        from meho_backplane.db.models import EndpointDescriptor
        from meho_backplane.operations._errors import (
            result_connector_error,
            result_invalid_params,
            result_unknown_op,
        )
        from meho_backplane.operations._handler_resolve import (
            import_handler,
            is_unbound_method,
        )
        from meho_backplane.operations._lookup import count_known_ops
        from meho_backplane.operations._validate import validate_params

        start = time.monotonic()

        def _elapsed() -> float:
            return (time.monotonic() - start) * 1000.0

        # Global-only descriptor lookup. The dispatcher's
        # ``lookup_descriptor`` takes an operator tenant_id for the
        # tenant-scoped-first fallback; the chassis path lacks one, so
        # we hit only the global row set. Typed registrations are
        # always global (``tenant_id IS NULL``) by construction.
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.tenant_id.is_(None),
                    EndpointDescriptor.product == self.product,
                    EndpointDescriptor.version == self.version,
                    EndpointDescriptor.impl_id == self.impl_id,
                    EndpointDescriptor.op_id == op_id,
                    EndpointDescriptor.is_enabled.is_(True),
                )
            )
            descriptor = result.scalar_one_or_none()

        if descriptor is None:
            known_op_count = await count_known_ops(
                product=self.product,
                version=self.version,
                impl_id=self.impl_id,
            )
            return result_unknown_op(op_id, known_op_count, _elapsed())

        # Parameter validation. The dispatcher runs this before the
        # policy gate; the shim runs it before invocation for the
        # same reason (cheap rejection of malformed inputs).
        validation_errors = validate_params(descriptor.parameter_schema, params)
        if validation_errors:
            return result_invalid_params(op_id, validation_errors, _elapsed())

        # Handler resolution + bound-method binding. ``import_handler``
        # walks the dotted path via importlib + getattr; the bound-
        # method shape (``module.ClassName.method``) returns the
        # unbound function, which we rebind against ``self``.
        handler = import_handler(descriptor.handler_ref or "")
        if is_unbound_method(handler, type(self)):
            handler = handler.__get__(self, type(self))

        try:
            raw = await handler(target=target, params=params)
        except Exception as exc:
            return result_connector_error(op_id, exc, _elapsed())

        return OperationResult(
            status="ok",
            op_id=op_id,
            result=raw if isinstance(raw, (dict, list)) else {"value": raw},
            duration_ms=_elapsed(),
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
