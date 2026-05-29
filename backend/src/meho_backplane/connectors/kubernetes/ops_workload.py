# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Workload K8s ops -- ``k8s.pod.{list,info}`` + ``k8s.deployment.{list,info}``.

G3.2-T3 (#323) of Initiative #320. T1 + T2 + T5 landed the connector
skeleton, the core inventory ops, and the logs op against the G0.6
substrate; this module adds the four highest-volume workload ops the
operator's daily ``kubectl-vcf.sh`` usage covers:

* ``k8s.pod.list [--namespace X | --all-namespaces]
  [--label-selector ...] [--field-selector ...] [--limit N]
  [--continue-token ...]`` -- one row per pod with name / namespace /
  status / ready / restarts / age / node / IP. Forwards the standard
  k8s ``label_selector`` / ``field_selector`` / ``limit`` /
  ``_continue`` query knobs to the API server so heavy-tenancy
  clusters can paginate server-side without pulling every row through
  the connector.
* ``k8s.pod.info <name> --namespace X`` -- full pod detail (spec
  containers / volumes / status conditions / container statuses / node
  assignment / QoS / init containers). Pod-name resolution accepts an
  exact match or a unique prefix; ambiguous prefixes return a
  structured error listing the candidates so the agent can disambiguate
  without a second namespace list.
* ``k8s.deployment.list [--namespace X | --all-namespaces]
  [--label-selector ...] [--limit N] [--continue-token ...]`` -- one
  row per deployment with name / namespace / replica counts / image /
  age / strategy. Same pagination shape as ``k8s.pod.list``.
* ``k8s.deployment.info <name> --namespace X`` -- full deployment
  detail (spec template / strategy / status replicas / conditions).
  Prefix resolution mirrors ``k8s.pod.info``.

Wire shape conventions
----------------------

The list handlers emit ``{"rows": [...], "total": N}`` plus an optional
``next_continue`` key when the server signals more pages via
``V1ListMeta._continue``. T2's module docstring spells out the
rationale: connector handlers stay reducer-agnostic (raw rows + total).
The dispatcher's default
:class:`meho_backplane.operations.jsonflux_reducer.JsonFluxReducer`
(installed in ``main.py`` via ``set_default_reducer``) owns set-shaped
truncation + handle creation; small payloads pass through verbatim into
:attr:`OperationResult.result`. The ``next_continue`` token is the same
server-side cursor the operator can pass back as ``continue_token`` for
the next page. See ``docs/architecture/jsonflux.md``.

The ``info`` handlers emit a flat dict structured around the API
object's spec + status. Pod info includes container statuses (with
ready / restartCount / lastState) because the operator's "is this pod
healthy?" question is the dominant ``kubectl describe pod`` use case.
Deployment info includes the rollout status (replicas / available /
ready / updated) because that's what ``kubectl rollout status`` reads.

Prefix resolution
-----------------

:func:`resolve_pod_name` and :func:`resolve_deployment_name` accept an
exact name first; a unique prefix second; otherwise raise
:class:`WorkloadNotFoundError` with the candidate list. The handler
maps the error to a structured ``OperationResult(status="error",
error="...")`` shape via the dispatcher's ``connector_error`` branch
(the exception class lands in ``extras.exception_class`` so callers
can render a "did-you-mean" hint without parsing the error string).

The resolution shape mirrors :mod:`ops_logs` -- both modules end up
listing the namespace's pods / deployments to resolve a prefix.
``k8s.pod.list`` itself uses the server-side ``limit`` /
``_continue`` knobs because the operator-typed query is unconstrained;
the prefix resolver bounds itself to the same namespace's pod set
which is typically O(10) in operator daily use.

References
----------
* Parent task: G3.2-T3 (#323).
* Parent Initiative: G3.2 (#320).
* Substrate: G0.6 (#388) typed-op registry + dispatcher.
* k8s Pod API:
  https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/pod-v1/
* k8s Deployment API:
  https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/deployment-v1/
* ``kubernetes_asyncio.CoreV1Api``:
  https://github.com/tomplus/kubernetes_asyncio/blob/master/kubernetes_asyncio/docs/CoreV1Api.md
* ``kubernetes_asyncio.AppsV1Api``:
  https://github.com/tomplus/kubernetes_asyncio/blob/master/kubernetes_asyncio/docs/AppsV1Api.md
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from kubernetes_asyncio import client

from meho_backplane.connectors.kubernetes.ops import KubernetesOp
from meho_backplane.connectors.kubernetes.ops_core import age_seconds
from meho_backplane.connectors.kubernetes.ops_listparams import (
    LIST_BASE_PROPERTIES,
    NAMESPACE_XOR_ALL_NAMESPACES,
)

if TYPE_CHECKING:
    from kubernetes_asyncio.client.models import (
        V1Container,
        V1ContainerStatus,
        V1Deployment,
        V1Pod,
    )

    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.kubernetes.connector import KubernetesConnector
    from meho_backplane.connectors.kubernetes.kubeconfig import KubernetesTargetLike

__all__ = [
    "K8S_DEPLOYMENT_INFO_LLM_INSTRUCTIONS",
    "K8S_DEPLOYMENT_INFO_PARAMETER_SCHEMA",
    "K8S_DEPLOYMENT_LIST_LLM_INSTRUCTIONS",
    "K8S_DEPLOYMENT_LIST_PARAMETER_SCHEMA",
    "K8S_POD_INFO_LLM_INSTRUCTIONS",
    "K8S_POD_INFO_PARAMETER_SCHEMA",
    "K8S_POD_LIST_LLM_INSTRUCTIONS",
    "K8S_POD_LIST_PARAMETER_SCHEMA",
    "WORKLOAD_OPS",
    "AmbiguousPrefixError",
    "WorkloadNotFoundError",
    "container_status_row",
    "deployment_info",
    "deployment_row",
    "k8s_deployment_info",
    "k8s_deployment_list",
    "k8s_pod_info",
    "k8s_pod_list",
    "pod_info",
    "pod_ready_string",
    "pod_row",
    "resolve_deployment_name",
    "resolve_pod_name",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class WorkloadNotFoundError(LookupError):
    """Workload object not found by exact name or unique prefix.

    Distinct from a generic :class:`LookupError` so the dispatcher's
    ``connector_error`` envelope carries
    ``extras.exception_class="WorkloadNotFoundError"`` and callers can
    render a not-found hint. ``candidates`` is always empty for this
    error (the prefix matched zero objects); the ambiguous case lives
    on :class:`AmbiguousPrefixError`.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class AmbiguousPrefixError(LookupError):
    """Workload prefix matched multiple objects.

    The candidate list is on :attr:`candidates` so the dispatcher's
    ``connector_error`` envelope can surface them without forcing the
    caller to parse the error string. Subclassing
    :class:`LookupError` (not :class:`ValueError`) keeps the
    name-resolution shape uniform with :class:`WorkloadNotFoundError`
    so dispatch-error renderers can branch once on
    ``isinstance(exc, LookupError)``.
    """

    def __init__(self, message: str, candidates: list[str]) -> None:
        super().__init__(message, candidates)

    @property
    def candidates(self) -> list[str]:
        return list(self.args[1])


# ---------------------------------------------------------------------------
# Pure row-shape helpers
# ---------------------------------------------------------------------------


def container_status_row(status: V1ContainerStatus) -> dict[str, Any]:
    """Project a :class:`V1ContainerStatus` into a flat dict.

    ``state`` and ``last_state`` are :class:`V1ContainerState` unions
    -- exactly one of ``running``, ``waiting``, ``terminated`` is
    non-null. The handler picks the populated branch's keyword (e.g.
    ``"running"`` / ``"waiting"`` / ``"terminated"``) so the caller
    sees the operator-visible string ``kubectl get pods`` prints,
    not the union nesting.
    """
    return {
        "name": status.name,
        "image": status.image,
        "ready": bool(status.ready),
        "restart_count": int(status.restart_count or 0),
        "state": _container_state_label(status.state),
        "last_state": _container_state_label(status.last_state),
    }


def _container_state_label(state: Any) -> str | None:
    """Pick the populated branch of a :class:`V1ContainerState` union.

    Returns ``"running"`` / ``"waiting"`` / ``"terminated"`` or
    ``None`` when the state is absent (e.g. ``last_state`` on a pod
    that has never restarted). Mirrors what ``kubectl get pods -o
    wide`` shows in its STATUS column.
    """
    if state is None:
        return None
    if state.running is not None:
        return "running"
    if state.waiting is not None:
        return "waiting"
    if state.terminated is not None:
        return "terminated"
    return None


def pod_ready_string(pod: V1Pod) -> str:
    """Format the ``READY`` column the way ``kubectl get pods`` prints it.

    The column is ``<ready>/<total>`` over containers (excluding init
    containers, per kubectl's convention). Init-container readiness is
    surfaced in the per-container statuses on the info path; the list
    column intentionally summarises only the main containers because
    that's what operators are looking at.
    """
    spec = pod.spec
    status = pod.status
    total = len(spec.containers) if spec is not None and spec.containers else 0
    ready = 0
    if status is not None and status.container_statuses:
        ready = sum(1 for cs in status.container_statuses if cs.ready)
    return f"{ready}/{total}"


def _pod_total_restarts(pod: V1Pod) -> int:
    """Sum the restart_count across all main containers.

    ``kubectl get pods`` shows the sum across containers; init-container
    restarts are tracked separately on the info path and would inflate
    the list column for sidecar-heavy pods.
    """
    status = pod.status
    if status is None or not status.container_statuses:
        return 0
    return sum(int(cs.restart_count or 0) for cs in status.container_statuses)


def pod_row(pod: V1Pod, *, now: datetime | None = None) -> dict[str, Any]:
    """Project a :class:`V1Pod` into the ``k8s.pod.list`` row shape.

    Pure mapping; the test suite pins it against synthetic
    :class:`V1Pod` instances without an event loop. ``status`` is the
    phase string (``Running`` / ``Pending`` / ``Succeeded`` /
    ``Failed`` / ``Unknown``); ``ready`` is the ``X/Y`` column ``kubectl
    get pods`` prints; ``restarts`` is the sum across main containers;
    ``node`` is the assigned node name (``None`` if the pod is
    pending-schedule); ``ip`` is the pod IP (``None`` until the
    network plugin assigns one).
    """
    metadata = pod.metadata
    status = pod.status
    spec = pod.spec
    return {
        "name": metadata.name if metadata is not None else None,
        "namespace": metadata.namespace if metadata is not None else None,
        "status": status.phase if status is not None else None,
        "ready": pod_ready_string(pod),
        "restarts": _pod_total_restarts(pod),
        "age_seconds": age_seconds(
            metadata.creation_timestamp if metadata is not None else None,
            now=now,
        ),
        "node": spec.node_name if spec is not None else None,
        "ip": status.pod_ip if status is not None else None,
    }


def _container_row(container: V1Container) -> dict[str, Any]:
    """Project a :class:`V1Container` into the info-path container dict.

    Ports / env / resources are surfaced as lightweight nested dicts so
    the operator sees what they need without the full openapi model
    shape. Volume mounts are projected by name + mount path; the full
    volume definition lives on the pod-level ``volumes`` key.
    """
    ports = [
        {
            "name": p.name,
            "container_port": p.container_port,
            "protocol": p.protocol,
            "host_port": p.host_port,
        }
        for p in (container.ports or [])
    ]
    env = [{"name": e.name, "value": e.value} for e in (container.env or [])]
    resources_block: dict[str, Any] = {}
    resources = container.resources
    if resources is not None:
        if resources.requests:
            resources_block["requests"] = dict(resources.requests)
        if resources.limits:
            resources_block["limits"] = dict(resources.limits)
    volume_mounts = [
        {
            "name": vm.name,
            "mount_path": vm.mount_path,
            "read_only": bool(vm.read_only) if vm.read_only is not None else False,
        }
        for vm in (container.volume_mounts or [])
    ]
    return {
        "name": container.name,
        "image": container.image,
        "ports": ports,
        "env": env,
        "resources": resources_block,
        "volume_mounts": volume_mounts,
    }


def _pod_volume_row(volume: Any) -> dict[str, Any]:
    """Project a :class:`V1Volume` into a flat dict.

    Surfaces the volume's name and the populated source-type label
    (``configMap``, ``secret``, ``persistentVolumeClaim``, ``emptyDir``,
    etc.). The full source spec stays out of the response -- operators
    almost always want "what kind of volume is mounted?" rather than the
    full source detail.
    """
    sources = (
        "config_map",
        "secret",
        "persistent_volume_claim",
        "empty_dir",
        "host_path",
        "projected",
        "downward_api",
        "csi",
        "ephemeral",
    )
    source_label: str | None = None
    for attr in sources:
        if getattr(volume, attr, None) is not None:
            source_label = attr.replace("_", "-")
            break
    return {"name": volume.name, "source": source_label}


def pod_info(pod: V1Pod, *, now: datetime | None = None) -> dict[str, Any]:
    """Project a :class:`V1Pod` into the ``k8s.pod.info`` full-detail shape.

    Flat top-level dict; nested dicts for spec/status sub-objects.
    Container statuses include the per-container readiness +
    restartCount + state so the operator can identify which container
    is unhealthy on a multi-container pod without a second round-trip.
    Init containers are surfaced separately because their state
    interpretation differs (an init container in ``terminated``/
    ``Completed`` is healthy; a main container in that state is not).
    """
    metadata = pod.metadata
    spec = pod.spec
    status = pod.status

    containers: list[dict[str, Any]] = []
    init_containers: list[dict[str, Any]] = []
    volumes: list[dict[str, Any]] = []
    if spec is not None:
        containers = [_container_row(c) for c in (spec.containers or [])]
        init_containers = [_container_row(c) for c in (spec.init_containers or [])]
        volumes = [_pod_volume_row(v) for v in (spec.volumes or [])]

    container_statuses: list[dict[str, Any]] = []
    init_container_statuses: list[dict[str, Any]] = []
    conditions: list[dict[str, Any]] = []
    if status is not None:
        container_statuses = [container_status_row(cs) for cs in (status.container_statuses or [])]
        init_container_statuses = [
            container_status_row(cs) for cs in (status.init_container_statuses or [])
        ]
        conditions = [
            {
                "type": cond.type,
                "status": cond.status,
                "reason": cond.reason,
                "message": cond.message,
            }
            for cond in (status.conditions or [])
        ]

    return {
        "name": metadata.name if metadata is not None else None,
        "namespace": metadata.namespace if metadata is not None else None,
        "status": status.phase if status is not None else None,
        "node": spec.node_name if spec is not None else None,
        "ip": status.pod_ip if status is not None else None,
        "host_ip": status.host_ip if status is not None else None,
        "qos_class": status.qos_class if status is not None else None,
        "age_seconds": age_seconds(
            metadata.creation_timestamp if metadata is not None else None,
            now=now,
        ),
        "labels": (metadata.labels or {}) if metadata is not None else {},
        "containers": containers,
        "init_containers": init_containers,
        "volumes": volumes,
        "container_statuses": container_statuses,
        "init_container_statuses": init_container_statuses,
        "conditions": conditions,
    }


def _deployment_strategy_label(deployment: V1Deployment) -> str | None:
    """Pick the operator-facing strategy label (``RollingUpdate`` / ``Recreate``)."""
    spec = deployment.spec
    if spec is None or spec.strategy is None:
        return None
    label: str | None = spec.strategy.type
    return label


def _deployment_first_image(deployment: V1Deployment) -> str | None:
    """Return the first container image from the deployment's pod template.

    The list path's ``image`` column shows one image (kubectl's
    convention is the first container in the template). Multi-container
    deployments surface the full template on the info path.
    """
    spec = deployment.spec
    if spec is None or spec.template is None or spec.template.spec is None:
        return None
    containers = spec.template.spec.containers or []
    if not containers:
        return None
    image: str | None = containers[0].image
    return image


def deployment_row(deployment: V1Deployment, *, now: datetime | None = None) -> dict[str, Any]:
    """Project a :class:`V1Deployment` into the list row shape.

    Replica counts come from ``status`` (the controller's observed
    state) rather than ``spec.replicas`` (the desired count) because
    operators want the live-state breakdown. ``replicas_desired``
    surfaces the spec value so the operator sees both targets in one
    row.
    """
    metadata = deployment.metadata
    spec = deployment.spec
    status = deployment.status
    return {
        "name": metadata.name if metadata is not None else None,
        "namespace": metadata.namespace if metadata is not None else None,
        "replicas_desired": int(spec.replicas)
        if spec is not None and spec.replicas is not None
        else 0,
        "replicas_ready": int(status.ready_replicas)
        if status is not None and status.ready_replicas is not None
        else 0,
        "replicas_available": int(status.available_replicas)
        if status is not None and status.available_replicas is not None
        else 0,
        "image": _deployment_first_image(deployment),
        "age_seconds": age_seconds(
            metadata.creation_timestamp if metadata is not None else None,
            now=now,
        ),
        "strategy": _deployment_strategy_label(deployment),
    }


def deployment_info(deployment: V1Deployment, *, now: datetime | None = None) -> dict[str, Any]:
    """Project a :class:`V1Deployment` into the ``k8s.deployment.info`` full-detail shape.

    Includes the pod template's container list (image / ports / env /
    resources / volume mounts), the rollout strategy block, the full
    status block (replica counts + observed generation), and the
    deployment conditions (``Available`` / ``Progressing`` /
    ``ReplicaFailure``). The conditions list is the operator's hook
    for "why is this deployment unhealthy?" -- ``status=False`` on
    ``Available`` carries a ``message`` the API server wrote.
    """
    metadata = deployment.metadata
    spec = deployment.spec
    status = deployment.status

    containers: list[dict[str, Any]] = []
    if spec is not None and spec.template is not None and spec.template.spec is not None:
        containers = [_container_row(c) for c in (spec.template.spec.containers or [])]

    strategy_block: dict[str, Any] = {}
    if spec is not None and spec.strategy is not None:
        strategy_block["type"] = spec.strategy.type
        if spec.strategy.rolling_update is not None:
            rolling = spec.strategy.rolling_update
            strategy_block["rolling_update"] = {
                "max_surge": str(rolling.max_surge) if rolling.max_surge is not None else None,
                "max_unavailable": str(rolling.max_unavailable)
                if rolling.max_unavailable is not None
                else None,
            }

    status_block: dict[str, Any] = {}
    conditions: list[dict[str, Any]] = []
    if status is not None:
        status_block = {
            "replicas": int(status.replicas) if status.replicas is not None else 0,
            "ready_replicas": int(status.ready_replicas)
            if status.ready_replicas is not None
            else 0,
            "available_replicas": int(status.available_replicas)
            if status.available_replicas is not None
            else 0,
            "updated_replicas": int(status.updated_replicas)
            if status.updated_replicas is not None
            else 0,
            "unavailable_replicas": int(status.unavailable_replicas)
            if status.unavailable_replicas is not None
            else 0,
            "observed_generation": int(status.observed_generation)
            if status.observed_generation is not None
            else 0,
        }
        conditions = [
            {
                "type": cond.type,
                "status": cond.status,
                "reason": cond.reason,
                "message": cond.message,
            }
            for cond in (status.conditions or [])
        ]

    return {
        "name": metadata.name if metadata is not None else None,
        "namespace": metadata.namespace if metadata is not None else None,
        "replicas_desired": int(spec.replicas)
        if spec is not None and spec.replicas is not None
        else 0,
        "age_seconds": age_seconds(
            metadata.creation_timestamp if metadata is not None else None,
            now=now,
        ),
        "labels": (metadata.labels or {}) if metadata is not None else {},
        "strategy": strategy_block,
        "containers": containers,
        "status": status_block,
        "conditions": conditions,
    }


# ---------------------------------------------------------------------------
# Prefix resolution
# ---------------------------------------------------------------------------


def _resolve_from_items(items: list[Any], target_name: str, kind: str) -> Any:
    """Pick the exact-match or unique-prefix match from *items*.

    Common logic shared between :func:`resolve_pod_name` and
    :func:`resolve_deployment_name`. Two-pass: exact match wins (so
    ``foo-bar`` matches ``foo-bar`` even when ``foo-bar-x`` also
    exists); otherwise the prefix matches collect into a candidate
    list. Zero candidates raises :class:`WorkloadNotFoundError`;
    multiple raise :class:`AmbiguousPrefixError` with the candidate
    list. *kind* parameterises the error string ("pod", "deployment").
    """
    exact_matches = [obj for obj in items if obj.metadata.name == target_name]
    if exact_matches:
        return exact_matches[0]

    prefix_matches = [obj for obj in items if obj.metadata.name.startswith(target_name)]
    if not prefix_matches:
        raise WorkloadNotFoundError(f"{kind} {target_name!r} not found")
    if len(prefix_matches) > 1:
        candidates = sorted(obj.metadata.name for obj in prefix_matches)
        raise AmbiguousPrefixError(
            f"{kind} prefix {target_name!r} matched {len(candidates)} objects",
            candidates,
        )
    return prefix_matches[0]


async def resolve_pod_name(v1: client.CoreV1Api, namespace: str, pod_name: str) -> V1Pod:
    """Resolve a pod name + namespace pair to a :class:`V1Pod`.

    Exact-match wins; otherwise tries ``pod_name`` as a prefix within
    the namespace. Raises :class:`WorkloadNotFoundError` /
    :class:`AmbiguousPrefixError`. The single ``list_namespaced_pod``
    call is unbounded server-side -- operator-facing namespaces are
    typically O(10..100) pods which is well within an unpaginated list
    -- but a heavy-tenancy namespace would benefit from a future
    field-selector + pagination shape (recorded in the module
    docstring's prefix-resolution note).
    """
    pod_list = await v1.list_namespaced_pod(namespace=namespace)
    return cast("V1Pod", _resolve_from_items(list(pod_list.items or []), pod_name, "pod"))


async def resolve_deployment_name(
    apps_v1: client.AppsV1Api, namespace: str, deployment_name: str
) -> V1Deployment:
    """Resolve a deployment name + namespace pair to a :class:`V1Deployment`."""
    deployment_list = await apps_v1.list_namespaced_deployment(namespace=namespace)
    return cast(
        "V1Deployment",
        _resolve_from_items(list(deployment_list.items or []), deployment_name, "deployment"),
    )


# ---------------------------------------------------------------------------
# Handlers -- module-level free functions
#
# The :class:`KubernetesConnector` bound-method shims forward into these so a
# future per-op-handler-file split (one ``ops_<verb>.py`` per op, without a
# class) keeps the API stable. Same shape :mod:`ops_logs` uses for its
# ``k8s.logs`` handler.
# ---------------------------------------------------------------------------


def _list_kwargs(params: dict[str, Any]) -> dict[str, Any]:
    """Translate the operator-facing list params into kubernetes_asyncio kwargs.

    The operator's ``--label-selector`` / ``--field-selector`` /
    ``--limit`` / ``--continue-token`` map to ``label_selector`` /
    ``field_selector`` / ``limit`` / ``_continue`` on the API. ``None``
    values are stripped so ``kubernetes_asyncio`` doesn't send empty
    filter strings to the API server (which would change query
    semantics on some k8s versions).
    """
    kwargs: dict[str, Any] = {
        "label_selector": params.get("label_selector"),
        "field_selector": params.get("field_selector"),
        "limit": params.get("limit"),
        "_continue": params.get("continue_token"),
    }
    return {k: v for k, v in kwargs.items() if v is not None}


def _next_continue_token(resp: Any) -> str | None:
    """Extract the server's continuation token from a list response.

    The token lives at ``resp.metadata._continue`` -- non-empty means
    "more pages exist; pass this back as ``continue_token`` to fetch
    them". The connector surfaces it under ``next_continue`` in the
    response dict so the operator-facing renderer can decide whether
    to fetch + concatenate or to expose a "fetch more" affordance.
    """
    metadata = getattr(resp, "metadata", None)
    if metadata is None:
        return None
    token: str | None = getattr(metadata, "_continue", None)
    if not token:
        return None
    return token


async def k8s_pod_list(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.pod.list``.

    Routes by ``all_namespaces`` (``list_pod_for_all_namespaces``) vs
    ``namespace`` (``list_namespaced_pod``). Returns
    ``{rows, total, next_continue?}``; the ``next_continue`` key is
    only present when the server signalled more pages exist. Schema
    validation runs in the dispatcher before this handler is called.

    ``operator`` is forwarded to
    :meth:`KubernetesConnector._get_api_client` so a cold-cache
    kubeconfig load runs under the operator's identity.
    """
    namespace: str | None = params.get("namespace")
    all_namespaces = bool(params.get("all_namespaces", False))
    api_client = await connector._get_api_client(target, operator)
    v1 = client.CoreV1Api(api_client)
    kwargs = _list_kwargs(params)
    if all_namespaces:
        resp = await v1.list_pod_for_all_namespaces(**kwargs)
    else:
        # Schema enforces exactly-one-of (namespace, all_namespaces); the
        # ``or ""`` cast is defensive against a schema relaxation that
        # would otherwise crash with a TypeError from kubernetes_asyncio.
        resp = await v1.list_namespaced_pod(namespace=namespace or "", **kwargs)
    rows = [pod_row(p) for p in (resp.items or [])]
    result: dict[str, Any] = {"rows": rows, "total": len(rows)}
    token = _next_continue_token(resp)
    if token is not None:
        result["next_continue"] = token
    return result


async def k8s_pod_info(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.pod.info``.

    Resolves the pod name (exact-match or unique-prefix) within the
    namespace and returns the full info dict. Reads the pod via
    :meth:`CoreV1Api.read_namespaced_pod` only when the resolved name
    differs from the input -- the prefix-resolution list response
    already contains the full :class:`V1Pod`, so the extra round-trip
    is skipped for the exact-name path.

    ``operator`` is forwarded to
    :meth:`KubernetesConnector._get_api_client` so a cold-cache
    kubeconfig load runs under the operator's identity.
    """
    pod_name: str = params["pod_name"]
    namespace: str = params["namespace"]
    api_client = await connector._get_api_client(target, operator)
    v1 = client.CoreV1Api(api_client)
    pod = await resolve_pod_name(v1, namespace, pod_name)
    # The list-derived V1Pod carries the full spec + status by default,
    # so we don't need a second ``read_namespaced_pod`` round-trip --
    # ``list_namespaced_pod`` returns the same object kubectl get
    # serves.
    return pod_info(pod)


async def k8s_deployment_list(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.deployment.list``.

    Same pagination + filter shape as ``k8s.pod.list``. The
    ``field_selector`` knob is less commonly used against deployments
    (no ``status.phase`` filter equivalent), but the API accepts it so
    we forward it for parity with the pod path.

    ``operator`` is forwarded to
    :meth:`KubernetesConnector._get_api_client` so a cold-cache
    kubeconfig load runs under the operator's identity.
    """
    namespace: str | None = params.get("namespace")
    all_namespaces = bool(params.get("all_namespaces", False))
    api_client = await connector._get_api_client(target, operator)
    apps_v1 = client.AppsV1Api(api_client)
    kwargs = _list_kwargs(params)
    if all_namespaces:
        resp = await apps_v1.list_deployment_for_all_namespaces(**kwargs)
    else:
        resp = await apps_v1.list_namespaced_deployment(namespace=namespace or "", **kwargs)
    rows = [deployment_row(d) for d in (resp.items or [])]
    result: dict[str, Any] = {"rows": rows, "total": len(rows)}
    token = _next_continue_token(resp)
    if token is not None:
        result["next_continue"] = token
    return result


async def k8s_deployment_info(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.deployment.info``. Mirrors :func:`k8s_pod_info`.

    ``operator`` is forwarded to
    :meth:`KubernetesConnector._get_api_client` so a cold-cache
    kubeconfig load runs under the operator's identity.
    """
    deployment_name: str = params["deployment_name"]
    namespace: str = params["namespace"]
    api_client = await connector._get_api_client(target, operator)
    apps_v1 = client.AppsV1Api(api_client)
    deployment = await resolve_deployment_name(apps_v1, namespace, deployment_name)
    return deployment_info(deployment)


# ---------------------------------------------------------------------------
# Parameter schemas + LLM instructions
# ---------------------------------------------------------------------------


#: ``k8s.pod.list`` parameter schema. The shared
#: :data:`~meho_backplane.connectors.kubernetes.ops_listparams.LIST_BASE_PROPERTIES`
#: + :data:`~meho_backplane.connectors.kubernetes.ops_listparams.NAMESPACE_XOR_ALL_NAMESPACES`
#: are the reference shape for every list op in this connector
#: (`docs/codebase/api-shape-conventions.md` §10). Pod / deployment
#: list adopt the full base; event / service / ingress / configmap
#: list adopt the subset that maps to their server-side API surface
#: (e.g. ``k8s.event.list`` keeps the namespace XOR + ``label_selector``
#: but omits ``continue_token`` -- the omission is documented in
#: :data:`~meho_backplane.connectors.kubernetes.ops_events.K8S_EVENT_LIST_PAGINATION_HINT`).
K8S_POD_LIST_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": LIST_BASE_PROPERTIES,
    "oneOf": NAMESPACE_XOR_ALL_NAMESPACES,
    "additionalProperties": False,
}


K8S_POD_INFO_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pod_name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Pod name. Exact match wins; otherwise treated as a "
                "prefix within the namespace. Ambiguous prefixes return "
                "a structured error listing the candidates."
            ),
        },
        "namespace": {
            "type": "string",
            "minLength": 1,
            "description": "Namespace the pod lives in.",
        },
    },
    "required": ["pod_name", "namespace"],
    "additionalProperties": False,
}


K8S_DEPLOYMENT_LIST_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": LIST_BASE_PROPERTIES,
    "oneOf": NAMESPACE_XOR_ALL_NAMESPACES,
    "additionalProperties": False,
}


K8S_DEPLOYMENT_INFO_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "deployment_name": {
            "type": "string",
            "minLength": 1,
            "description": ("Deployment name. Exact match or unique prefix within the namespace."),
        },
        "namespace": {
            "type": "string",
            "minLength": 1,
            "description": "Namespace the deployment lives in.",
        },
    },
    "required": ["deployment_name", "namespace"],
    "additionalProperties": False,
}


#: Pagination hint surfaced by the JsonFlux reducer on every reducing
#: ``k8s.pod.list`` response. G0.15-T8 (#1219): consumers reading the
#: reduced envelope's ``fetch_more.native_pagination`` block see the
#: param vocabulary + a curated example next call without re-deriving
#: the contract. ``continue_token`` is the connector-side name for the
#: server's ``_continue`` cursor (the connector renames it on the way
#: out so the operator-facing surface stays kubectl-shaped); the hint
#: documents both so a consumer can map either direction.
K8S_POD_LIST_PAGINATION_HINT: dict[str, Any] = {
    "params": {
        "continue_token": (
            "Server-emitted pagination cursor from the prior response's "
            "``next_continue`` field. Pass back unchanged to fetch the "
            "next page; stale tokens (>5..15 min) return 410 -- restart "
            "the list without it."
        ),
        "label_selector": (
            "Standard k8s label selector (e.g. ``app=argocd-server``, "
            "``app in (frontend,backend)``). Forwarded server-side."
        ),
        "field_selector": (
            "Standard k8s field selector (e.g. ``status.phase=Running``, "
            "``spec.nodeName=node-1``). Forwarded server-side."
        ),
        "namespace": "Narrow to one namespace.",
        "limit": "Server-side page size (1..1000).",
    },
    "example_next_call": {
        "tool": "call_operation",
        "args": {
            "op_id": "k8s.pod.list",
            "params": {
                "all_namespaces": True,
                "field_selector": "status.phase!=Running",
            },
        },
    },
}


K8S_POD_LIST_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Call when the operator asks 'what pods are running?' or wants "
        "the kubectl-equivalent of 'kubectl get pods -n <ns>'. Pass "
        "label_selector / field_selector to narrow (e.g. "
        "field_selector='status.phase=Running'). Use all_namespaces=true "
        "for cluster-wide questions. Combine limit + continue_token for "
        "explicit pagination on heavy-tenancy clusters."
    ),
    "parameter_hints": {
        "namespace": "Required unless all_namespaces is true.",
        "all_namespaces": "Pass true for cluster-wide listings.",
        "label_selector": ("k8s selector syntax (e.g. 'app=argocd-server', 'role in (cp, etcd)')."),
        "field_selector": (
            "Server-side filter. Common: 'status.phase=Running', 'spec.nodeName=node-1'."
        ),
        "limit": "Cap rows per response. Combine with continue_token.",
        "continue_token": "Pass the prior response's next_continue verbatim.",
    },
    "output_shape": (
        "{'rows': [{name, namespace, status, ready, restarts, "
        "age_seconds, node, ip}], 'total': <int>, 'next_continue': "
        "<str | absent>}. ``ready`` is the kubectl 'X/Y' string; "
        "``restarts`` is the sum across main containers. "
        "``next_continue`` is present iff the server has more pages."
    ),
    "pagination_hint": K8S_POD_LIST_PAGINATION_HINT,
}


K8S_POD_INFO_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Call when the operator names a specific pod and wants the "
        "kubectl-describe equivalent: spec containers, volumes, status "
        "conditions, per-container statuses, node assignment, QoS, "
        "init-container detail. Accepts a prefix (e.g. 'argocd-server') "
        "as long as it matches one pod in the namespace; "
        "ambiguous prefixes return a candidates list."
    ),
    "parameter_hints": {
        "pod_name": "Exact name or unique prefix within the namespace.",
        "namespace": "Required.",
    },
    "output_shape": (
        "Flat dict: {name, namespace, status, node, ip, host_ip, "
        "qos_class, age_seconds, labels, containers, init_containers, "
        "volumes, container_statuses, init_container_statuses, "
        "conditions}. ``container_statuses`` carries per-container "
        "ready/restart_count/state for unhealthy-container diagnosis."
    ),
}


#: Same shape as :data:`K8S_POD_LIST_PAGINATION_HINT` -- the k8s server-
#: side pagination contract is identical across list ops. G0.15-T8
#: (#1219). The example_next_call uses a deployment-shaped narrowing
#: filter so an agent reading this hint doesn't carry pod-shaped
#: intuition over.
K8S_DEPLOYMENT_LIST_PAGINATION_HINT: dict[str, Any] = {
    "params": {
        "continue_token": (
            "Server-emitted pagination cursor from the prior response's "
            "``next_continue`` field. Pass back unchanged to fetch the "
            "next page; stale tokens return 410."
        ),
        "label_selector": "Standard k8s label selector. Forwarded server-side.",
        "field_selector": "Standard k8s field selector. Forwarded server-side.",
        "namespace": "Narrow to one namespace.",
        "limit": "Server-side page size (1..1000).",
    },
    "example_next_call": {
        "tool": "call_operation",
        "args": {
            "op_id": "k8s.deployment.list",
            "params": {
                "namespace": "kube-system",
                "label_selector": "app in (coredns,kube-proxy)",
            },
        },
    },
}


K8S_DEPLOYMENT_LIST_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Call when the operator asks 'what's deployed in <ns>?' or "
        "wants 'kubectl get deployments'. Same pagination + selector "
        "shape as k8s.pod.list. Use to read rollout state across the "
        "namespace; for a single deployment's full detail call "
        "k8s.deployment.info."
    ),
    "parameter_hints": {
        "namespace": "Required unless all_namespaces is true.",
        "all_namespaces": "Pass true for cluster-wide listings.",
        "label_selector": "k8s selector syntax.",
        "limit": "Cap rows per response. Combine with continue_token.",
        "continue_token": "Pass the prior response's next_continue verbatim.",
    },
    "output_shape": (
        "{'rows': [{name, namespace, replicas_desired, replicas_ready, "
        "replicas_available, image, age_seconds, strategy}], 'total', "
        "'next_continue'?}. ``image`` is the first container's image "
        "(kubectl convention); full template is on the info path."
    ),
    "pagination_hint": K8S_DEPLOYMENT_LIST_PAGINATION_HINT,
}


K8S_DEPLOYMENT_INFO_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Call when the operator names a specific deployment and wants "
        "the full template + rollout status: pod-template containers, "
        "strategy (RollingUpdate maxSurge/maxUnavailable), live replica "
        "counts, conditions (Available / Progressing / ReplicaFailure). "
        "Prefix resolution mirrors k8s.pod.info."
    ),
    "parameter_hints": {
        "deployment_name": "Exact name or unique prefix within the namespace.",
        "namespace": "Required.",
    },
    "output_shape": (
        "Flat dict: {name, namespace, replicas_desired, age_seconds, "
        "labels, strategy, containers, status, conditions}. ``status`` "
        "is the live replica breakdown (replicas / ready / available / "
        "updated / unavailable / observed_generation)."
    ),
}


# ---------------------------------------------------------------------------
# Response schemas -- informational; the dispatcher's default reducer
# (JsonFluxReducer) does not validate outbound payloads against them.
# ---------------------------------------------------------------------------


_POD_ROW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": ["string", "null"]},
        "namespace": {"type": ["string", "null"]},
        "status": {"type": ["string", "null"]},
        "ready": {"type": "string"},
        "restarts": {"type": "integer"},
        "age_seconds": {"type": ["integer", "null"]},
        "node": {"type": ["string", "null"]},
        "ip": {"type": ["string", "null"]},
    },
    "required": ["name", "namespace", "status", "ready", "restarts", "age_seconds", "node", "ip"],
    "additionalProperties": False,
}


_K8S_POD_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": {"type": "array", "items": _POD_ROW_SCHEMA},
        "total": {"type": "integer"},
        "next_continue": {"type": "string"},
    },
    "required": ["rows", "total"],
    "additionalProperties": False,
}


_K8S_POD_INFO_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Flat pod detail; see llm_instructions.output_shape.",
    "additionalProperties": True,
}


_DEPLOYMENT_ROW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": ["string", "null"]},
        "namespace": {"type": ["string", "null"]},
        "replicas_desired": {"type": "integer"},
        "replicas_ready": {"type": "integer"},
        "replicas_available": {"type": "integer"},
        "image": {"type": ["string", "null"]},
        "age_seconds": {"type": ["integer", "null"]},
        "strategy": {"type": ["string", "null"]},
    },
    "required": [
        "name",
        "namespace",
        "replicas_desired",
        "replicas_ready",
        "replicas_available",
        "image",
        "age_seconds",
        "strategy",
    ],
    "additionalProperties": False,
}


_K8S_DEPLOYMENT_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": {"type": "array", "items": _DEPLOYMENT_ROW_SCHEMA},
        "total": {"type": "integer"},
        "next_continue": {"type": "string"},
    },
    "required": ["rows", "total"],
    "additionalProperties": False,
}


_K8S_DEPLOYMENT_INFO_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Flat deployment detail; see llm_instructions.output_shape.",
    "additionalProperties": True,
}


WORKLOAD_OPS: tuple[KubernetesOp, ...] = (
    KubernetesOp(
        op_id="k8s.pod.list",
        handler_attr="k8s_pod_list",
        summary="List pods in a namespace (or cluster-wide) with selectors + pagination.",
        description=(
            "Calls ``CoreV1Api.list_namespaced_pod`` (or "
            "``list_pod_for_all_namespaces`` when ``all_namespaces=true``) "
            "and projects each pod into a flat row {name, namespace, "
            "status, ready, restarts, age_seconds, node, ip}. Forwards "
            "the standard k8s ``label_selector`` / ``field_selector`` / "
            "``limit`` / ``_continue`` query knobs to the API server "
            "so heavy-tenancy clusters can paginate server-side. The "
            "response includes ``next_continue`` when the server signals "
            "more pages; pass it back as ``continue_token`` to walk."
        ),
        parameter_schema=K8S_POD_LIST_PARAMETER_SCHEMA,
        response_schema=_K8S_POD_LIST_RESPONSE_SCHEMA,
        group_key="workload",
        tags=("read-only", "pod", "workload"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=K8S_POD_LIST_LLM_INSTRUCTIONS,
    ),
    KubernetesOp(
        op_id="k8s.pod.info",
        handler_attr="k8s_pod_info",
        summary="Full pod detail by exact name or unique prefix.",
        description=(
            "Resolves ``pod_name`` against the namespace (exact match "
            "wins; otherwise unique prefix) and returns the full spec + "
            "status snapshot the operator's ``kubectl describe pod`` "
            "habit reads: containers (image / ports / env / resources / "
            "volume mounts), init containers, volumes (name + source "
            "type), per-container statuses (ready / restart_count / "
            "state), conditions, node assignment, QoS class. Ambiguous "
            "prefixes return a structured error listing the candidates."
        ),
        parameter_schema=K8S_POD_INFO_PARAMETER_SCHEMA,
        response_schema=_K8S_POD_INFO_RESPONSE_SCHEMA,
        group_key="workload",
        tags=("read-only", "pod", "workload"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=K8S_POD_INFO_LLM_INSTRUCTIONS,
    ),
    KubernetesOp(
        op_id="k8s.deployment.list",
        handler_attr="k8s_deployment_list",
        summary="List deployments with live replica counts + image + strategy.",
        description=(
            "Calls ``AppsV1Api.list_namespaced_deployment`` (or "
            "``list_deployment_for_all_namespaces``) and projects each "
            "row down to {name, namespace, replicas_desired, "
            "replicas_ready, replicas_available, image, age_seconds, "
            "strategy}. ``image`` is the first container's image "
            "(kubectl convention; full template lives on the info path). "
            "Same server-side pagination shape as ``k8s.pod.list``."
        ),
        parameter_schema=K8S_DEPLOYMENT_LIST_PARAMETER_SCHEMA,
        response_schema=_K8S_DEPLOYMENT_LIST_RESPONSE_SCHEMA,
        group_key="workload",
        tags=("read-only", "deployment", "workload"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=K8S_DEPLOYMENT_LIST_LLM_INSTRUCTIONS,
    ),
    KubernetesOp(
        op_id="k8s.deployment.info",
        handler_attr="k8s_deployment_info",
        summary="Full deployment detail by exact name or unique prefix.",
        description=(
            "Resolves ``deployment_name`` against the namespace and "
            "returns the full template + rollout status: pod-template "
            "containers, strategy block (RollingUpdate's maxSurge / "
            "maxUnavailable when present), live replica breakdown "
            "(replicas / ready / available / updated / unavailable / "
            "observed_generation), and deployment conditions "
            "(``Available`` / ``Progressing`` / ``ReplicaFailure``) "
            "with reason + message. Prefix resolution mirrors "
            "``k8s.pod.info``."
        ),
        parameter_schema=K8S_DEPLOYMENT_INFO_PARAMETER_SCHEMA,
        response_schema=_K8S_DEPLOYMENT_INFO_RESPONSE_SCHEMA,
        group_key="workload",
        tags=("read-only", "deployment", "workload"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=K8S_DEPLOYMENT_INFO_LLM_INSTRUCTIONS,
    ),
)
