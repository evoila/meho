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
  ``("k8s", "1.x", "k8s")``. The single-impl ``impl_id == product``
  shape mirrors the Vault sibling and round-trips through
  :func:`~meho_backplane.operations._lookup.parse_connector_id` for
  the canonical ``connector_id="k8s-1.x"`` shape. The shipped v1 entry
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

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.system_operator import synthesise_system_operator
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
    TopologyHints,
)

__all__ = ["KubernetesConnector", "product_from_git_version"]


_log = structlog.get_logger(__name__)

_DEFAULT_K8S_PORT = 6443
_PROBE_TIMEOUT_SECONDS = 5.0
_PROBE_OK_STATUSES = frozenset({200, 401})


#: Curated agent-actionable group selectors. Surfaced verbatim by
#: ``list_operation_groups`` (G0.6-T8 #399) so the LLM client can pick
#: a group before drilling into ``search_operations``. The strings
#: differentiate groups from one another -- each entry explicitly
#: names the *kind of question* that routes here and the cross-group
#: pairing pattern with the rest of the K8s surface. T4b (#732)
#: curated; T4a (#731) is the structural sibling that makes the
#: ``when_to_use`` kwarg required on
#: :func:`~meho_backplane.operations.typed_register.register_typed_operation`.
_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    "cluster": (
        "Use for cluster-identity questions before any per-resource "
        "drill-in: 'which K8s flavour / distribution / version is this "
        "target running?' The single ``k8s.about`` op returns the "
        "product slug (rke2 / k3s / eks / gke / aks / vanilla) plus "
        "git_version. Call this first when the agent needs to pick a "
        "version-flavoured doc page from the knowledge base or to "
        "decide whether RKE2-specific vs vanilla-K8s behaviour applies "
        "to a downstream op."
    ),
    "inventory": (
        "Use for cluster-wide 'what is in this cluster?' questions -- "
        "namespace and node enumeration plus the namespace-scoped "
        "``k8s.ls`` walker. The right group when the agent doesn't yet "
        "know which namespace to target. Pair with the 'workload' "
        "group once a namespace is identified (``k8s.ls /<ns>`` "
        "returns kind -> count summaries; drill into per-pod / per-"
        "deployment detail via workload-group ops)."
    ),
    "workload": (
        "Use for per-namespace pod and deployment drill-in: "
        "``k8s.pod.list`` / ``k8s.pod.info`` / ``k8s.deployment.list`` "
        "/ ``k8s.deployment.info``. The right group once the operator "
        "knows the namespace (typically picked from the 'inventory' "
        "group first). Pair with the 'logs' group when investigating "
        "a CrashLoopBackOff pod, with 'events' when the failure is "
        "scheduler- or admission-controller-driven, and with 'network' "
        "to map a workload's Services and Ingresses."
    ),
    "network": (
        "Use for service-routing and ingress questions: "
        "``k8s.service.list`` (ClusterIP / NodePort / LoadBalancer "
        "with endpoint counts) and ``k8s.ingress.list`` (hostname + "
        "path -> backend mappings). The right group for 'how is this "
        "workload exposed?' or 'which hostname routes to which "
        "service?'. Pair with the 'workload' group to map back to "
        "the pods behind each Service via label selectors."
    ),
    "config": (
        "Use for ConfigMap data inspection: ``k8s.configmap.list`` "
        "(keys-only -- one row per ConfigMap with its data keys but "
        "no values) and ``k8s.configmap.info`` (full data payload for "
        "one named ConfigMap). The right group for 'what config is "
        "this workload reading?' or 'which env var lives in which "
        "ConfigMap?'. The list / info split is deliberate -- the "
        "agent surveys keys cheaply via list, then fetches values "
        "only for the specific ConfigMap it needs."
    ),
    "events": (
        "Use for cluster-event observability and troubleshooting: "
        "``k8s.event.list`` returns the recent Event stream "
        "(scheduler decisions, admission-controller rejections, "
        "image-pull failures, OOMKilled signals, FailedMount, "
        "BackOff). The right group when a workload's status looks "
        "wrong and the agent needs the *why*. Pair with the 'workload' "
        "group to scope events to one pod / deployment, and with the "
        "'logs' group when the event points at a container-internal "
        "failure rather than a K8s-control-plane one."
    ),
    "logs": (
        "Use for container stdout / stderr inspection: ``k8s.logs`` "
        "fetches a non-streaming chunk (kubectl-style --tail / "
        "--container / --since / --previous knobs). The right group "
        "once the agent has identified a specific pod (typically from "
        "'workload' or 'events') and needs the application's own log "
        "output. Streaming follow-mode ('kubectl logs -f') is "
        "deliberately out of scope -- request bounded chunks."
    ),
}


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

    # Registry v2 metadata. The single-impl ``impl_id == product``
    # shape mirrors the Vault sibling (decision #8 -- the library name
    # ``kubernetes_asyncio`` lives in the package layout +
    # ``pyproject.toml`` dependency, not the registry triple). The
    # triple ``("k8s", "1.x", "k8s")`` round-trips through
    # :func:`~meho_backplane.operations._lookup.parse_connector_id`
    # for the canonical ``connector_id="k8s-1.x"`` shape every
    # operator surface (CLI alias verbs / meta-tool dispatch) bakes.
    # A future EKS-specific transport lands as a sibling row under
    # ``("k8s", "1.x", "<impl-id>")`` and selects via
    # ``target.preferred_impl_id`` -- the same shape vmware-rest's
    # multi-impl-ready substrate already supports.
    product = "k8s"
    version = "1.x"
    impl_id = "k8s"

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

    async def fingerprint(
        self,
        target: KubernetesTargetLike,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Canonical fingerprint built from ``VersionApi.get_code()``.

        ``operator`` is the request-scoped operator when the fingerprint
        runs from an authenticated probe route (the REST
        ``POST /api/v1/targets/{name}/probe`` route and the UI
        ``POST /ui/connectors/{name}/probe`` re-probe button both lift
        an :class:`~meho_backplane.auth.operator.Operator` from the
        chassis JWT chain and pass it here). Forwarding the operator
        means the underlying :class:`KubeconfigLoader` reads the
        per-target kubeconfig under the **same identity** the dispatch
        path uses — Vault's JWT/OIDC auth method gets a valid Keycloak
        token and serves the read instead of rejecting it as
        ``malformed jwt: must have three parts`` (the placeholder JWT
        the system operator carried).

        ``operator=None`` is reserved for background / system-initiated
        callers that have no real operator in scope (the readiness probe
        worker, the K8s topology refresh). In that case the connector
        synthesises a system operator whose placeholder JWT fails closed
        at the live Vault loader — preserving the architectural posture
        that *system-initiated calls cannot perform an operator-context
        Vault read* (the locked Option A decision in
        :doc:`docs/architecture/connector-auth.md`).

        G0.16-T4 (#1306) converged this path with the dispatch surface
        — both now flow the operator through the same
        :func:`~meho_backplane.connectors.kubernetes.kubeconfig.load_kubeconfig_from_vault`
        helper. See :doc:`docs/codebase/connectors-kubernetes.md`.
        """
        eff_operator = operator if operator is not None else synthesise_system_operator()
        api_client = await self._get_api_client(target, eff_operator)
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
        operator: Operator,
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

        ``operator`` is the dispatch op's operator; it is forwarded to
        :meth:`_get_api_client` so a cold-cache kubeconfig load happens
        under the operator's identity (the locked Option A decision —
        per-target Vault read under the operator's Identity entity).
        ``dispatch_typed`` passes ``operator`` here because the signature
        names it. The returned dict is intentionally flat -- no nested
        ``extras`` -- so the dispatcher's default
        :class:`meho_backplane.operations.jsonflux_reducer.JsonFluxReducer`
        passes this scalar (non-set-shaped) payload through verbatim
        into ``OperationResult.result`` with a ``None`` handle; only
        set-shaped responses above the threshold are materialized into a
        :class:`~meho_backplane.connectors.schemas.ResultHandle`.
        """
        del params  # declared in schema; the handler intentionally ignores them
        api_client = await self._get_api_client(target, operator)
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

    async def discover_topology(
        self,
        target: KubernetesTargetLike,
        *,
        operator: Operator | None = None,
    ) -> TopologyHints:
        """Return cluster + namespaces + nodes as a :class:`TopologyHints`.

        Op-side: this is the G0.14-T12 (#1201) populator the refresh
        service (:func:`meho_backplane.topology.refresh.refresh_target_topology`)
        calls on demand and on the per-tenant scheduled tick. The
        returned snapshot is diffed against the existing ``graph_node`` +
        ``graph_edge`` rows for ``(operator.tenant_id, target.id)`` and
        applied as inserts / updates / soft-deletes.

        Scope (deliberately minimal for v0.7):

        * one ``target`` :class:`NodeHint` for the cluster — properties
          carry the K8s server version, the same ``VersionApi.get_code()``
          payload :meth:`about` already issues. ``cluster`` is not in the
          v0.2 :data:`NodeKind` enum (the enum is closed per
          ``connectors/schemas.py``) so the cluster manifests as
          ``kind="target"``.
        * one ``namespace`` :class:`NodeHint` per namespace — properties
          from :func:`namespace_row` (``status`` / ``age_seconds`` /
          ``labels``).
        * one ``node`` :class:`NodeHint` per cluster node — properties
          from :func:`node_row` (``roles`` / ``version`` /
          ``kernel``…).
        * one ``belongs-to`` :class:`EdgeHint` from each namespace and
          each cluster node to the target node.

        Pods / services / ingresses / deployments / volumes are
        **explicitly out of scope** — each would multiply the
        per-refresh API-call cost in proportion to the namespace count.
        Sibling Tasks land them when refresh-cost data justifies the
        spend.

        ``operator`` is keyword-only with a ``None`` default to keep the
        :class:`Connector` ABC signature unchanged while letting the
        refresh service forward the per-tenant system operator the
        scheduler synthesises
        (:func:`~meho_backplane.topology.scheduler._system_operator`).
        The forwarded operator flows through :meth:`_get_api_client` so
        the operator-context Vault → kubeconfig chain reads under the
        scheduler's synthetic tenant identity. The ``None`` fall-back
        synthesises the system operator the fingerprint/probe paths
        already use, so a direct caller (test, future on-demand surface
        that doesn't yet thread the operator) still works closed-loop.
        """
        from meho_backplane.connectors.kubernetes._topology import build_topology_hints

        eff_operator = operator if operator is not None else synthesise_system_operator()
        api_client = await self._get_api_client(target, eff_operator)
        version_api = client.VersionApi(api_client)
        core_v1 = client.CoreV1Api(api_client)
        version = await version_api.get_code()
        namespaces_resp = await core_v1.list_namespace()
        nodes_resp = await core_v1.list_node()
        return build_topology_hints(
            target_name=target.name,
            version=version,
            namespaces=list(namespaces_resp.items or []),
            nodes=list(nodes_resp.items or []),
        )

    async def k8s_namespace_list(
        self,
        operator: Operator,
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

        ``operator`` is forwarded to :meth:`_get_api_client` so a
        cold-cache kubeconfig load runs under the operator's identity;
        see :meth:`about` for the threading rationale. ``params`` is
        declared empty in the op's
        :attr:`~meho_backplane.connectors.kubernetes.ops_core.K8S_NAMESPACE_LIST_PARAMETER_SCHEMA`;
        the dispatcher's :class:`Draft202012Validator` rejects any extra
        keys before this handler runs.
        """
        del params
        api_client = await self._get_api_client(target, operator)
        core_v1 = client.CoreV1Api(api_client)
        resp = await core_v1.list_namespace()
        rows = [namespace_row(ns) for ns in resp.items]
        return {"rows": rows, "total": len(rows)}

    async def k8s_node_list(
        self,
        operator: Operator,
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

        ``operator`` is forwarded to :meth:`_get_api_client`; see
        :meth:`about` for the threading rationale.
        """
        del params
        api_client = await self._get_api_client(target, operator)
        core_v1 = client.CoreV1Api(api_client)
        resp = await core_v1.list_node()
        rows = [node_row(n) for n in resp.items]
        return {"rows": rows, "total": len(rows)}

    async def k8s_service_list(
        self,
        operator: Operator,
        target: KubernetesTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """List services per-namespace or cluster-wide.

        Op-id: ``k8s.service.list``. Branches on ``all_namespaces``
        between ``CoreV1Api.list_namespaced_service(namespace, ...)``
        and ``CoreV1Api.list_service_for_all_namespaces(...)``
        (G0.17-T1 #1330) and projects each :class:`V1Service` through
        :func:`~meho_backplane.connectors.kubernetes.ops_network.service_row`.
        The helper is pure so the unit suite pins the wire shape
        against synthetic fixtures without booting an event loop.
        Forwards ``label_selector`` verbatim to the API.

        ``operator`` is forwarded to :meth:`_get_api_client`; see
        :meth:`about` for the threading rationale.
        """
        from meho_backplane.connectors.kubernetes.ops_network import service_row

        namespace: str | None = params.get("namespace")
        all_namespaces = bool(params.get("all_namespaces", False))
        label_selector = params.get("label_selector")
        api_client = await self._get_api_client(target, operator)
        core_v1 = client.CoreV1Api(api_client)
        kwargs: dict[str, Any] = {}
        if label_selector:
            kwargs["label_selector"] = label_selector
        if all_namespaces:
            resp = await core_v1.list_service_for_all_namespaces(**kwargs)
        else:
            # Schema enforces XOR; ``or ""`` is defensive against a
            # future schema relaxation.
            resp = await core_v1.list_namespaced_service(namespace=namespace or "", **kwargs)
        rows = [service_row(s) for s in resp.items]
        return {"rows": rows, "total": len(rows)}

    async def k8s_ingress_list(
        self,
        operator: Operator,
        target: KubernetesTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """List ingresses per-namespace or cluster-wide.

        Op-id: ``k8s.ingress.list``. Branches on ``all_namespaces``
        between
        ``NetworkingV1Api.list_namespaced_ingress(namespace, ...)``
        and ``NetworkingV1Api.list_ingress_for_all_namespaces(...)``
        (G0.17-T1 #1330) and projects each :class:`V1Ingress` through
        :func:`~meho_backplane.connectors.kubernetes.ops_network.ingress_row`.
        Forwards ``label_selector`` verbatim to the API.

        ``operator`` is forwarded to :meth:`_get_api_client`; see
        :meth:`about` for the threading rationale.
        """
        from meho_backplane.connectors.kubernetes.ops_network import ingress_row

        namespace: str | None = params.get("namespace")
        all_namespaces = bool(params.get("all_namespaces", False))
        label_selector = params.get("label_selector")
        api_client = await self._get_api_client(target, operator)
        networking_v1 = client.NetworkingV1Api(api_client)
        kwargs: dict[str, Any] = {}
        if label_selector:
            kwargs["label_selector"] = label_selector
        if all_namespaces:
            resp = await networking_v1.list_ingress_for_all_namespaces(**kwargs)
        else:
            resp = await networking_v1.list_namespaced_ingress(namespace=namespace or "", **kwargs)
        rows = [ingress_row(i) for i in resp.items]
        return {"rows": rows, "total": len(rows)}

    async def k8s_configmap_list(
        self,
        operator: Operator,
        target: KubernetesTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """List configmaps per-namespace or cluster-wide -- **keys only, no values**.

        Op-id: ``k8s.configmap.list``. Branches on ``all_namespaces``
        between
        ``CoreV1Api.list_namespaced_config_map(namespace, ...)`` and
        ``CoreV1Api.list_config_map_for_all_namespaces(...)``
        (G0.17-T1 #1330) and projects each :class:`V1ConfigMap`
        through
        :func:`~meho_backplane.connectors.kubernetes.ops_config.configmap_list_row`,
        which deliberately omits ``data`` / ``binary_data`` values.
        The privacy contract holds identically across both paths.
        Operators wanting values call ``k8s.configmap.info`` per
        configmap so the audit row records the targeted read.
        Forwards ``label_selector`` verbatim to the API.

        ``operator`` is forwarded to :meth:`_get_api_client`; see
        :meth:`about` for the threading rationale.
        """
        from meho_backplane.connectors.kubernetes.ops_config import configmap_list_row

        namespace: str | None = params.get("namespace")
        all_namespaces = bool(params.get("all_namespaces", False))
        label_selector = params.get("label_selector")
        api_client = await self._get_api_client(target, operator)
        core_v1 = client.CoreV1Api(api_client)
        kwargs: dict[str, Any] = {}
        if label_selector:
            kwargs["label_selector"] = label_selector
        if all_namespaces:
            resp = await core_v1.list_config_map_for_all_namespaces(**kwargs)
        else:
            resp = await core_v1.list_namespaced_config_map(namespace=namespace or "", **kwargs)
        rows = [configmap_list_row(cm) for cm in resp.items]
        return {"rows": rows, "total": len(rows)}

    async def k8s_configmap_info(
        self,
        operator: Operator,
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

        ``operator`` is forwarded to :meth:`_get_api_client`; see
        :meth:`about` for the threading rationale.
        """
        from meho_backplane.connectors.kubernetes.ops_config import configmap_info

        api_client = await self._get_api_client(target, operator)
        core_v1 = client.CoreV1Api(api_client)
        cm = await core_v1.read_namespaced_config_map(
            name=params["name"], namespace=params["namespace"]
        )
        return configmap_info(cm)

    async def k8s_event_list(
        self,
        operator: Operator,
        target: KubernetesTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """List events most-recent-first, truncated to ``limit``; per-namespace or cluster-wide.

        Op-id: ``k8s.event.list``. Branches on ``all_namespaces``
        between ``CoreV1Api.list_namespaced_event(namespace, ...)``
        (default) and ``CoreV1Api.list_event_for_all_namespaces(...)``
        (G0.17-T1 #1330 -- the cluster-wide
        ``kubectl get events -A`` operator question) and projects each
        :class:`CoreV1Event` through
        :func:`~meho_backplane.connectors.kubernetes.ops_events.event_row`.
        Per-row ``namespace`` already flows through ``event_row`` so
        cross-namespace results stay distinguishable.

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
        operator is better served by a ``field_selector`` /
        ``label_selector`` than by a bigger result set (the K8s API
        itself paginates above ~1k items per response in most
        deployments).
        """
        from meho_backplane.connectors.kubernetes.ops_events import (
            DEFAULT_EVENT_LIMIT,
            MAX_EVENT_LIMIT,
            event_row,
            sort_event_rows_recent_first,
        )

        namespace: str | None = params.get("namespace")
        all_namespaces = bool(params.get("all_namespaces", False))
        limit = int(params.get("limit", DEFAULT_EVENT_LIMIT))
        # Defence in depth: the schema's ``maximum`` already enforces
        # the cap. Keep the explicit clamp so a future schema relaxation
        # cannot exceed the ceiling silently -- same discipline as
        # ``k8s.logs``'s tail clamp.
        if limit > MAX_EVENT_LIMIT:
            limit = MAX_EVENT_LIMIT
        field_selector = params.get("field_selector")
        label_selector = params.get("label_selector")

        api_client = await self._get_api_client(target, operator)
        core_v1 = client.CoreV1Api(api_client)
        # Always pull up to MAX_EVENT_LIMIT rows so the client-side
        # sort sees the full recency-relevant superset; the caller's
        # ``limit`` truncates after the sort. See the docstring above
        # for the ordering rationale.
        kwargs: dict[str, Any] = {"limit": MAX_EVENT_LIMIT}
        if field_selector:
            kwargs["field_selector"] = field_selector
        if label_selector:
            kwargs["label_selector"] = label_selector
        if all_namespaces:
            resp = await core_v1.list_event_for_all_namespaces(**kwargs)
        else:
            # Schema enforces exactly-one-of (namespace, all_namespaces);
            # the ``or ""`` cast is defensive against a future schema
            # relaxation that would otherwise crash with a TypeError
            # from kubernetes_asyncio.
            resp = await core_v1.list_namespaced_event(namespace=namespace or "", **kwargs)
        rows = [event_row(e) for e in resp.items]
        sorted_rows = sort_event_rows_recent_first(rows)
        truncated = sorted_rows[:limit]
        return {"rows": truncated, "total": len(truncated)}

    async def k8s_ls(
        self,
        operator: Operator,
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
        the default
        :class:`meho_backplane.operations.jsonflux_reducer.JsonFluxReducer`
        handle creation, audit row, broadcast -- runs verbatim. The
        forwarding handler is structural plumbing, not a semantic
        shortcut.

        ``operator`` is forwarded to :meth:`_get_api_client` (and to the
        sub-op forwarder); see :meth:`about` for the threading rationale.
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
            return await self._k8s_ls_root(target, operator)
        if len(segments) == 1:
            return await self._k8s_ls_namespace(target, operator, segments[0])
        if len(segments) == 2:
            return await self._k8s_ls_namespace_kind(target, segments[0], segments[1])
        # Deeper paths aren't a documented v0.2 shape; collapse to the
        # namespace/kind forwarder against the first two segments. The
        # operator gets a useful result rather than an opaque error.
        return await self._k8s_ls_namespace_kind(target, segments[0], segments[1])

    async def _k8s_ls_root(
        self,
        target: KubernetesTargetLike,
        operator: Operator,
    ) -> dict[str, Any]:
        """Cluster-root view: list namespace names + the fixed cluster-kind list."""
        api_client = await self._get_api_client(target, operator)
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
        operator: Operator,
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
        api_client = await self._get_api_client(target, operator)
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
        operator: Operator,
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

        ``operator`` is forwarded to the module-level handler so it can
        reach the cached :class:`ApiClient` (cold-cache load happens
        under the operator's identity). Schema validation runs in the
        dispatcher before this method is called; the function-level
        handler re-reads only validated values.
        """
        from meho_backplane.connectors.kubernetes.ops_logs import k8s_logs

        return await k8s_logs(self, target, operator, params)

    async def k8s_pod_list(
        self,
        operator: Operator,
        target: KubernetesTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for the ``k8s.pod.list`` op (G3.2-T3 #323).

        Delegates to
        :func:`~meho_backplane.connectors.kubernetes.ops_workload.k8s_pod_list`.
        Same module-level-function shape :meth:`logs` uses so a future
        per-op-handler-file split keeps the registration API stable.

        ``operator`` is forwarded to the module-level handler; see
        :meth:`about` for the threading rationale.
        """
        from meho_backplane.connectors.kubernetes.ops_workload import (
            k8s_pod_list as _k8s_pod_list,
        )

        return await _k8s_pod_list(self, target, operator, params)

    async def k8s_pod_info(
        self,
        operator: Operator,
        target: KubernetesTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for the ``k8s.pod.info`` op (G3.2-T3 #323).

        ``operator`` is forwarded to the module-level handler; see
        :meth:`about` for the threading rationale.
        """
        from meho_backplane.connectors.kubernetes.ops_workload import (
            k8s_pod_info as _k8s_pod_info,
        )

        return await _k8s_pod_info(self, target, operator, params)

    async def k8s_deployment_list(
        self,
        operator: Operator,
        target: KubernetesTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for the ``k8s.deployment.list`` op (G3.2-T3 #323).

        ``operator`` is forwarded to the module-level handler; see
        :meth:`about` for the threading rationale.
        """
        from meho_backplane.connectors.kubernetes.ops_workload import (
            k8s_deployment_list as _k8s_deployment_list,
        )

        return await _k8s_deployment_list(self, target, operator, params)

    async def k8s_deployment_info(
        self,
        operator: Operator,
        target: KubernetesTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for the ``k8s.deployment.info`` op (G3.2-T3 #323).

        ``operator`` is forwarded to the module-level handler; see
        :meth:`about` for the threading rationale.
        """
        from meho_backplane.connectors.kubernetes.ops_workload import (
            k8s_deployment_info as _k8s_deployment_info,
        )

        return await _k8s_deployment_info(self, target, operator, params)

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
            when_to_use: str | None
            if op.group_key is None:
                when_to_use = None
            else:
                when_to_use = _WHEN_TO_USE_BY_GROUP.get(op.group_key)
                if when_to_use is None:
                    raise ValueError(
                        f"KubernetesConnector op {op.op_id!r} declares "
                        f"group_key={op.group_key!r} but no curated "
                        f"when_to_use exists for that key. Add an entry "
                        f"to _WHEN_TO_USE_BY_GROUP in "
                        f"meho_backplane.connectors.kubernetes.connector "
                        f"so list_operation_groups surfaces a real "
                        f"selection signal instead of the auto-derive "
                        f"template."
                    )
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
                when_to_use=when_to_use,
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
        from this entry point. To match the operator-aware handler
        signatures the dispatcher now passes ``operator`` to, the shim
        synthesises a system :class:`Operator` with an empty
        ``raw_jwt`` (the same shape the fingerprint / probe paths use,
        :func:`~meho_backplane.connectors._shared.system_operator.synthesise_system_operator`).
        On a cold kubeconfig-cache miss this fails closed with a clear
        error rather than silently authenticating as a backplane
        identity — the system-call carve-out from
        :doc:`docs/architecture/connector-auth.md`. The shim's
        intra-connector callers (the ``k8s.ls /<ns>/<kind>`` forwarder)
        run *after* the operator-aware op already warmed the
        ``ApiClient`` cache, so the synthesised operator never reaches
        the loader in practice; the fail-closed shape is the defensive
        case. The operator-aware surface is
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
        import inspect

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

        # Signature introspection mirroring ``dispatch_typed``: the
        # operator-aware handlers (every k8s op after #948) name
        # ``operator`` in their signature so the dispatcher knows to
        # forward it. The shim is operator-less, so it synthesises a
        # system operator (empty ``raw_jwt``); the kubeconfig loader's
        # fail-closed guard surfaces a clear error if the cache misses.
        # The ``raw``/no-``operator``-named branch stays in place so
        # the same shim works against an older handler that hasn't
        # been re-shaped.
        param_names = list(inspect.signature(handler).parameters.keys())
        try:
            if "operator" in param_names:
                raw = await handler(
                    operator=synthesise_system_operator(), target=target, params=params
                )
            else:
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

    async def _get_api_client(
        self,
        target: KubernetesTargetLike,
        operator: Operator,
    ) -> client.ApiClient:
        """Resolve (and cache) the :class:`ApiClient` for *target*.

        The single lock serialises concurrent first-use for any target;
        in practice the second caller hits the cache fast-path. The
        slow kubeconfig read happens under the lock so two concurrent
        callers for the same target don't both pay the cost.

        ``operator`` is forwarded to the injected
        :class:`~meho_backplane.connectors.kubernetes.kubeconfig.KubeconfigLoader`
        so the default
        :func:`~meho_backplane.connectors.kubernetes.kubeconfig.load_kubeconfig_from_vault`
        can read the per-target kubeconfig under the operator's identity
        (``vault_client_for_operator(operator)``). An injected test
        loader receives the same ``(target, operator)`` pair so the
        wiring is exercised by both the default and the injected path.

        Cache-hit fast path: when the :class:`ApiClient` is already
        cached for this target's :meth:`_cache_key`, the operator
        argument is ignored (the kubeconfig has already been resolved
        under a prior operator's identity). This is the v0.2 design
        choice: kubeconfigs are tied to ``target.secret_ref`` (the
        shared service account) rather than the acting operator, so the
        client is shareable across operators. A future per-operator
        auth model (impersonation) would re-key the cache on operator
        identity; until then ``secret_ref`` is sufficient.
        """
        key = self._cache_key(target)
        async with self._lock:
            cached = self._api_clients.get(key)
            if cached is not None:
                return cached
            kubeconfig_dict = await self._kubeconfig_loader(target, operator)
            api_client = await config.new_client_from_config_dict(kubeconfig_dict)
            self._api_clients[key] = api_client
            _log.info(
                "kubernetes_api_client_built",
                target=target.name,
                host=target.host,
            )
            return api_client
