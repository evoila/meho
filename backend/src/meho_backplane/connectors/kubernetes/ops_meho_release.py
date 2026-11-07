# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``k8s.meho.release.info`` -- what a MEHO install on this cluster runs today.

A read-only op. It answers "which MEHO runs here, which chart, which
revisions, is it ready?" in one call, and changes nothing. The parts:

* ``deployment``: the image, tag and digest of the backplane Deployment. The
  chart names it after the release (``demo-meho`` for release ``demo``,
  ``meho`` for release ``meho``, or ``fullnameOverride``), so the op does
  not use the name. It lists the Deployments with the label
  ``app.kubernetes.io/instance=<release>`` and takes the one with a
  container named ``backplane`` (``deploy/charts/meho/templates``). When
  the image has no digest, the digest comes from the running pods: only the
  ``backplane`` container, and only pods that run the same image as the
  Deployment. Pods of two builds at once give no ``digest`` but a
  ``pod_digests`` list.
* ``helm_chart``: the ``HelmChart`` object ``helm.cattle.io/v1`` named
  ``<release>`` (RKE2 installs): chart version and a hash of its values. It
  is read in the install namespace first. When it is not there, the op
  lists the HelmCharts of the cluster and takes the one named
  ``<release>`` whose ``spec.targetNamespace`` is the install namespace
  (the RKE2 layout with the chart in ``kube-system``).
* ``helm_releases``: the revisions of the Helm release, read from the
  labels of the Helm release Secrets (``owner=helm,name=<release>``).
* ``meho``: only when ``host`` is given. The answers of ``GET /version`` and
  ``GET /ready`` of that host, including ``db_ahead`` when the database is
  newer than the image.

Each part has a ``state``: ``present``, ``not present`` or ``unknown``. A
missing object is ``not present``. A read that failed (no right, server
error, timeout) is ``unknown`` with a short ``error``: it never looks like
"not there". A 401 is raised, so the dispatcher can renew the login.

Secrets: the op reads only the names and labels of the Helm release
Secrets. It never copies their ``data`` (which holds the values text) and
never returns the values of the ``HelmChart``, only a hash. The flight
recorder masks Secret data and ``HelmChart`` values before it stores a
trace (``redaction.py``, ``_wire_span.py``).

``host`` is an address the caller gives. It carries
``x-meho-caller-address: true``, so MEHO runs the op at risk level
``caution`` and waits for approval (``operations/caller_address.py``). The
op still changes nothing. ``host`` is dialed through the address-pinning
transport, so the same address check as for a target applies
(``MEHO_TARGET_SSRF_ALLOWLIST``).

References
----------
* Helm stores a release in a Secret ``sh.helm.release.v1.<name>.v<N>`` with
  the labels ``owner=helm``, ``name``, ``status``, ``version``:
  https://helm.sh/docs/topics/advanced/#storage-backends
* k3s / RKE2 ``HelmChart`` custom resource (``spec.targetNamespace``,
  ``spec.valuesContent``, ``spec.set``; the release name is the object name):
  https://docs.rke2.io/helm and
  https://github.com/k3s-io/helm-controller/blob/master/pkg/apis/helm.cattle.io/v1/types.go
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import httpx
from kubernetes_asyncio import client
from kubernetes_asyncio.client.exceptions import ApiException

from meho_backplane.connectors._shared.dial_guard import screen_dial_addresses
from meho_backplane.connectors._shared.pinned_transport import build_pinned_async_transport
from meho_backplane.connectors.kubernetes.ops import KubernetesOp

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.kubernetes.connector import KubernetesConnector
    from meho_backplane.connectors.kubernetes.kubeconfig import KubernetesTargetLike

__all__ = [
    "K8S_MEHO_RELEASE_INFO_LLM_INSTRUCTIONS",
    "K8S_MEHO_RELEASE_INFO_PARAMETER_SCHEMA",
    "K8S_MEHO_RELEASE_INFO_RESPONSE_SCHEMA",
    "OPS",
    "k8s_meho_release_info",
    "parse_image",
    "values_hash",
]

_NOT_PRESENT = "not present"
_PRESENT = "present"
_UNKNOWN = "unknown"

_DEFAULT_RELEASE = "meho"
_HELM_CHART_GROUP = "helm.cattle.io"
_HELM_CHART_VERSION = "v1"
_HELM_CHART_PLURAL = "helmcharts"

#: The chart's labels and container name (``deploy/charts/meho/templates``).
#: Every object of the release carries the instance label; only the
#: backplane Deployment has a container with this name.
_INSTANCE_LABEL = "app.kubernetes.io/instance"
_BACKPLANE_CONTAINER = "backplane"

#: Objects per page and the most pages read per list. A reached cap sets
#: ``incomplete``.
_PAGE_SIZE = 200
_MAX_PAGES = 10

_HTTP_TIMEOUT_SECONDS = 10.0
_HOST_PATTERN = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?$"
)
_USERINFO_PATTERN = re.compile(r"//[^/@\s]*@")
_VERSION_KEYS = ("git_sha", "build_date", "chart_version", "connector_packages")


def parse_image(image: str) -> dict[str, str | None]:
    """Split an image reference into ``repository``, ``tag`` and ``digest``."""
    ref, _, digest = image.partition("@")
    last_segment = ref.rsplit("/", 1)[-1]
    tag: str | None = None
    if ":" in last_segment:
        ref, _, tag = ref.rpartition(":")
    return {"repository": ref, "tag": tag, "digest": digest or None}


def values_hash(spec: dict[str, Any]) -> str:
    """Return the sha256 of the values of a ``HelmChart`` spec (never the values)."""
    material = {"valuesContent": spec.get("valuesContent"), "set": spec.get("set")}
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def _unknown(exc: Exception) -> dict[str, Any]:
    """A part that could not be read: its state is unknown, with a short reason."""
    if isinstance(exc, ApiException):
        return {"state": _UNKNOWN, "error": f"HTTP {exc.status}"}
    return {"state": _UNKNOWN, "error": type(exc).__name__}


def _not_found_or_unknown(exc: Exception) -> dict[str, Any]:
    """404 is ``not present``; a 401 is raised; anything else is ``unknown``."""
    if isinstance(exc, ApiException):
        if exc.status == 401:
            raise exc
        if exc.status == 404:
            return {"state": _NOT_PRESENT}
    return _unknown(exc)


def _items_and_token(resp: Any) -> tuple[list[Any], str | None]:
    """Items and continue token of one list page: a model or a plain dict."""
    if isinstance(resp, dict):
        metadata = resp.get("metadata") or {}
        return list(resp.get("items") or []), metadata.get("continue") or None
    return list(resp.items or []), resp.metadata._continue if resp.metadata else None


async def _list_all(
    call: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any
) -> tuple[list[Any], bool]:
    """Follow every page of a list call, up to the cap. Returns (items, cap reached)."""
    rows: list[Any] = []
    token: str | None = None
    for _page in range(_MAX_PAGES):
        page_kwargs: dict[str, Any] = {**kwargs, "limit": _PAGE_SIZE}
        if token:
            page_kwargs["_continue"] = token
        items, token = _items_and_token(await call(*args, **page_kwargs))
        rows.extend(items)
        if not token:
            return rows, False
    return rows, True


def _backplane_container(deployment: Any) -> Any | None:
    template = deployment.spec.template if deployment.spec else None
    containers = (template.spec.containers if template and template.spec else None) or []
    return next((c for c in containers if c.name == _BACKPLANE_CONTAINER), None)


def _container_digest(pod: Any, image: str) -> str | None:
    """Digest of the backplane container of *pod*, when it runs *image*.

    A pod of an older rollout runs another image and gives nothing. A
    sidecar is never used: only the status of the backplane container counts.
    """
    containers = (pod.spec.containers if pod.spec else None) or []
    if not any(c.name == _BACKPLANE_CONTAINER and c.image == image for c in containers):
        return None
    for status in (pod.status.container_statuses if pod.status else None) or []:
        if status.name == _BACKPLANE_CONTAINER:
            _, _, digest = (status.image_id or "").partition("@")
            return digest if digest.startswith("sha256:") else None
    return None


async def _pod_digests(
    core: client.CoreV1Api, namespace: str, selector: dict[str, str], image: str
) -> set[str]:
    """Digests the backplane pods of the Deployment run, from their ``imageID``."""
    label_selector = ",".join(f"{k}={v}" for k, v in sorted(selector.items()))
    pods, _ = await _list_all(core.list_namespaced_pod, namespace, label_selector=label_selector)
    found = (_container_digest(pod, image) for pod in pods)
    return {digest for digest in found if digest}


async def _add_pod_digest(
    api_client: client.ApiClient, namespace: str, deployment: Any, part: dict[str, Any]
) -> None:
    """Fill ``digest`` from the pods; two builds at once give ``pod_digests``."""
    selector = (deployment.spec.selector.match_labels or {}) if deployment.spec.selector else {}
    if not selector:
        return
    try:
        digests = await _pod_digests(
            client.CoreV1Api(api_client), namespace, selector, part["image"]
        )
    except Exception as exc:
        if isinstance(exc, ApiException) and exc.status == 401:
            raise
        part["digest_error"] = _unknown(exc)["error"]
        return
    if len(digests) == 1:
        part["digest"] = digests.pop()
    elif digests:
        part["pod_digests"] = sorted(digests)


async def _deployment_part(
    api_client: client.ApiClient, namespace: str, release: str
) -> dict[str, Any]:
    try:
        deployments, _ = await _list_all(
            client.AppsV1Api(api_client).list_namespaced_deployment,
            namespace,
            label_selector=f"{_INSTANCE_LABEL}={release}",
        )
    except Exception as exc:
        return _not_found_or_unknown(exc)
    matches = [d for d in deployments if _backplane_container(d) is not None]
    if not matches:
        return {"state": _NOT_PRESENT}
    if len(matches) > 1:
        names = sorted(d.metadata.name for d in matches if d.metadata)
        return {"state": _UNKNOWN, "error": "several Deployments match", "candidates": names}
    deployment = matches[0]
    main = _backplane_container(deployment)
    part: dict[str, Any] = {"state": _PRESENT, "name": deployment.metadata.name}
    status = deployment.status
    part["replicas"] = status.replicas if status else None
    part["ready_replicas"] = status.ready_replicas if status else None
    if main is None or not main.image:
        return {**part, "image": None, "tag": None, "digest": None}
    part.update(
        image=main.image, **{k: v for k, v in parse_image(main.image).items() if k != "repository"}
    )
    if part["digest"] is None:
        await _add_pod_digest(api_client, namespace, deployment, part)
    return part


def _chart_targets(obj: Any, namespace: str, release: str) -> bool:
    """``True`` when the ``HelmChart`` *obj* is the release in *namespace*."""
    if not isinstance(obj, dict):
        return False
    metadata = obj.get("metadata") or {}
    spec = obj.get("spec") or {}
    target = spec.get("targetNamespace") or metadata.get("namespace")
    return metadata.get("name") == release and target == namespace


async def _find_helm_chart(
    custom: client.CustomObjectsApi, namespace: str, release: str
) -> dict[str, Any]:
    """The chart in the install namespace, else the cluster's chart that targets it.

    Returns ``{"object": <chart>}``, or a ``not present`` / ``unknown`` part.
    """
    try:
        obj = await custom.get_namespaced_custom_object(
            group=_HELM_CHART_GROUP,
            version=_HELM_CHART_VERSION,
            namespace=namespace,
            plural=_HELM_CHART_PLURAL,
            name=release,
        )
    except Exception as exc:
        local = _not_found_or_unknown(exc)
        if local["state"] != _NOT_PRESENT:
            return local
    else:
        if _chart_targets(obj, namespace, release):
            return {"object": obj}
    try:
        charts, incomplete = await _list_all(
            custom.list_cluster_custom_object,
            _HELM_CHART_GROUP,
            _HELM_CHART_VERSION,
            _HELM_CHART_PLURAL,
        )
    except Exception as exc:
        return _not_found_or_unknown(exc)
    match = next((c for c in charts if _chart_targets(c, namespace, release)), None)
    if match is None:
        return {"state": _NOT_PRESENT, "incomplete": incomplete}
    return {"object": match}


async def _helm_chart_part(
    api_client: client.ApiClient, namespace: str, release: str
) -> dict[str, Any]:
    found = await _find_helm_chart(client.CustomObjectsApi(api_client), namespace, release)
    if "object" not in found:
        return found
    obj = found["object"]
    spec = obj.get("spec") or {}
    chart = spec.get("chart")
    return {
        "state": _PRESENT,
        "name": release,
        "chart_namespace": (obj.get("metadata") or {}).get("namespace"),
        "chart": _USERINFO_PATTERN.sub("//", chart) if isinstance(chart, str) else None,
        "version": spec.get("version"),
        "values_hash": values_hash(spec),
    }


def _revision_row(secret: Any) -> dict[str, Any]:
    labels = secret.metadata.labels or {}
    raw = labels.get("version", "")
    return {
        "revision": int(raw) if raw.isdigit() else None,
        "status": labels.get("status"),
        "secret_name": secret.metadata.name,
        "modified_at": labels.get("modifiedAt"),
    }


async def _helm_releases_part(
    api_client: client.ApiClient, namespace: str, release: str
) -> dict[str, Any]:
    try:
        secrets, incomplete = await _list_all(
            client.CoreV1Api(api_client).list_namespaced_secret,
            namespace,
            label_selector=f"owner=helm,name={release}",
        )
    except Exception as exc:
        if isinstance(exc, ApiException) and exc.status == 401:
            raise
        return _unknown(exc)
    rows = [_revision_row(s) for s in secrets]
    if not rows:
        return {"state": _NOT_PRESENT}
    rows.sort(key=lambda r: (r["revision"] is None, r["revision"] or 0))
    return {"state": _PRESENT, "revisions": rows, "incomplete": incomplete}


async def _get_json(http: httpx.AsyncClient, url: str) -> dict[str, Any]:
    """GET *url*; return ``{state, http_status, body}`` or ``{state: unknown, error}``."""
    try:
        resp = await http.get(url)
    except (httpx.HTTPError, OSError, ValueError) as exc:
        return {"state": _UNKNOWN, "error": type(exc).__name__}
    try:
        body = resp.json()
    except ValueError:
        body = None
    return {
        "state": _PRESENT,
        "http_status": resp.status_code,
        "body": body if isinstance(body, dict) else None,
    }


def _version_part(got: dict[str, Any]) -> dict[str, Any]:
    if got["state"] != _PRESENT:
        return got
    body = got["body"]
    if got["http_status"] != 200 or body is None:
        return {"state": _UNKNOWN, "error": f"HTTP {got['http_status']}"}
    return {"state": _PRESENT, **{k: body[k] for k in _VERSION_KEYS if k in body}}


def _ready_part(got: dict[str, Any]) -> dict[str, Any]:
    if got["state"] != _PRESENT:
        return got
    body = got["body"]
    if body is None:
        return {"state": _UNKNOWN, "error": f"HTTP {got['http_status']}"}
    checks = [c for c in body.get("checks") or [] if isinstance(c, dict)]
    db_ahead: bool | None = None
    for check in checks:
        if check.get("name") == "db":
            db_ahead = "db_ahead=true" in str(check.get("detail") or "")
    return {
        "state": _PRESENT,
        "http_status": got["http_status"],
        "status": body.get("status"),
        "ready": got["http_status"] == 200 and body.get("status") == "ready",
        "db_ahead": db_ahead,
        "checks": [{"name": c.get("name"), "ok": c.get("ok")} for c in checks],
    }


async def _meho_part(host: str) -> dict[str, Any]:
    transport = build_pinned_async_transport(screen_dial_addresses)
    async with httpx.AsyncClient(transport=transport, timeout=_HTTP_TIMEOUT_SECONDS) as http:
        version = await _get_json(http, f"https://{host}/version")
        ready = await _get_json(http, f"https://{host}/ready")
    return {"host": host, "version": _version_part(version), "ready": _ready_part(ready)}


async def k8s_meho_release_info(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.meho.release.info``. Reads only; changes nothing."""
    namespace: str = params["namespace"]
    release: str = params.get("release") or _DEFAULT_RELEASE
    host: str | None = params.get("host")
    if host is not None and not _HOST_PATTERN.match(host):
        raise ValueError("host must be a host name or IP address with an optional port")
    api_client = await connector._get_api_client(target, operator)
    result: dict[str, Any] = {
        "namespace": namespace,
        "release": release,
        "deployment": await _deployment_part(api_client, namespace, release),
        "helm_chart": await _helm_chart_part(api_client, namespace, release),
        "helm_releases": await _helm_releases_part(api_client, namespace, release),
    }
    if host is not None:
        result["meho"] = await _meho_part(host)
    return result


# ---------------------------------------------------------------------------
# Parameter schema, response schema, LLM instructions, registration
# ---------------------------------------------------------------------------

K8S_MEHO_RELEASE_INFO_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "namespace": {
            "type": "string",
            "minLength": 1,
            "description": "Namespace the MEHO install lives in.",
        },
        "release": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Helm release name: the value of the app.kubernetes.io/instance label "
                "and the name of the HelmChart object. Default: meho."
            ),
        },
        "host": {
            "type": "string",
            "minLength": 1,
            "x-meho-caller-address": True,
            "description": (
                "Optional host name or IP address (with an optional :port) of this MEHO "
                "install. When set, the result carries its /version and /ready answers. "
                "HTTPS is used."
            ),
        },
    },
    "required": ["namespace"],
    "additionalProperties": False,
    "examples": [
        {"namespace": "meho"},
        {"namespace": "meho", "release": "meho", "host": "<meho-host>"},
    ],
}

_PART: dict[str, Any] = {
    "type": "object",
    "properties": {
        "state": {"type": "string", "enum": [_PRESENT, _NOT_PRESENT, _UNKNOWN]},
        "error": {"type": "string"},
    },
    "required": ["state"],
    "additionalProperties": True,
}

K8S_MEHO_RELEASE_INFO_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "namespace": {"type": "string"},
        "release": {"type": "string"},
        "deployment": {
            **_PART,
            "description": (
                "name, image, tag, digest, replicas, ready_replicas. pod_digests when the "
                "pods run two builds of the image; digest_error when the pods could not be "
                "read; candidates when several Deployments match."
            ),
        },
        "helm_chart": {
            **_PART,
            "description": "name, chart_namespace, chart, version, values_hash.",
        },
        "helm_releases": {
            **_PART,
            "description": "revisions (revision, status, secret_name, modified_at), incomplete.",
        },
        "meho": {
            "type": "object",
            "description": "Only with host: host, version (/version), ready (/ready, db_ahead).",
            "additionalProperties": True,
        },
    },
    "required": ["namespace", "release", "deployment", "helm_chart", "helm_releases"],
    "additionalProperties": False,
}

K8S_MEHO_RELEASE_INFO_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to see which MEHO runs on a Kubernetes target: image tag and digest, "
        "chart version, Helm revisions, and whether it is ready. Use it before a "
        "MEHO update, to know what to go back to. It changes nothing. Waits for "
        "approval, because host is an address you give."
    ),
    "preconditions": (
        "The MEHO install must be in the given namespace. Pass host to also read "
        "/version and /ready; the host must pass the same address check as a target."
    ),
    "parameter_hints": {
        "namespace": "Required.",
        "release": (
            "Optional. Default 'meho'. The Deployment is found by its "
            "app.kubernetes.io/instance label, not by its name."
        ),
        "host": "Optional, for example '<meho-host>' or '<meho-host>:8443'.",
    },
    "output_shape": (
        "{namespace, release, deployment, helm_chart, helm_releases, meho?}. Each part "
        "has state 'present', 'not present' or 'unknown' (with error). 'not present' "
        "is normal: helm_chart exists only on RKE2 installs, helm_releases only on "
        "installs made with 'helm install'. deployment.digest is null with "
        "pod_digests when the pods run two builds at once. meho.ready.db_ahead true means the "
        "database is newer than the running image: the older chart still runs, and a "
        "database restore is not needed to go back. Never contains Secret data or "
        "values text, only names, revision numbers, status and a values hash."
    ),
    "next_step": (
        "Compare the digest and chart version with the version you want. For the "
        "running answers use meho.version."
    ),
}

MEHO_RELEASE_OPS: tuple[KubernetesOp, ...] = (
    KubernetesOp(
        op_id="k8s.meho.release.info",
        handler_attr="k8s_meho_release_info",
        summary="Show what a MEHO install runs: image, chart, Helm revisions, /version and /ready.",
        description=(
            "Changes nothing. Reads the MEHO Deployment (image tag and digest; found by its "
            "release label), the HelmChart object (chart version, values hash; also in "
            "another namespace), the Helm release Secret labels (revision, status) and, "
            "for a given host, /version and /ready. Missing is 'not present'."
        ),
        parameter_schema=K8S_MEHO_RELEASE_INFO_PARAMETER_SCHEMA,
        response_schema=K8S_MEHO_RELEASE_INFO_RESPONSE_SCHEMA,
        group_key="cluster",
        tags=("read-only", "release", "helm", "meho"),
        # ``host`` is an address the caller gives, so MEHO applies the floor
        # (caution + approval, ``operations/caller_address.py``) either way.
        safety_level="caution",
        requires_approval=True,
        llm_instructions=K8S_MEHO_RELEASE_INFO_LLM_INSTRUCTIONS,
    ),
)

#: Collected by :func:`~meho_backplane.connectors.kubernetes.ops.collect_ops`.
OPS: tuple[KubernetesOp, ...] = MEHO_RELEASE_OPS
