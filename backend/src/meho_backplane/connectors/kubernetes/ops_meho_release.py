# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``k8s.meho.release.info`` -- what a MEHO install on this cluster runs today.

A read-only op. It answers "which MEHO runs here, which chart, which
revisions, is it ready?" in one call, and changes nothing. The parts:

* ``deployment``: the image, tag and digest of the ``<release>`` Deployment
  (``AppsV1Api.read_namespaced_deployment``). When the image has no digest,
  the digest of a running pod (its ``imageID``) is used.
* ``helm_chart``: the ``HelmChart`` object ``helm.cattle.io/v1`` named
  ``<release>`` (RKE2 installs): chart version and a hash of its values.
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
recorder masks Secret data before it stores a trace.

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
* k3s / RKE2 ``HelmChart`` custom resource:
  https://docs.rke2.io/helm
"""

from __future__ import annotations

import hashlib
import json
import re
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

#: Secrets per page and the most pages read. A reached cap sets ``incomplete``.
_SECRET_PAGE_SIZE = 200
_SECRET_MAX_PAGES = 10

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


async def _pod_digest(
    core: client.CoreV1Api, namespace: str, selector: dict[str, str]
) -> str | None:
    """Digest of the image a pod of the Deployment runs, from its ``imageID``."""
    label_selector = ",".join(f"{k}={v}" for k, v in sorted(selector.items()))
    pods = await core.list_namespaced_pod(namespace, label_selector=label_selector, limit=10)
    for pod in pods.items or []:
        for status in (pod.status.container_statuses if pod.status else None) or []:
            _, _, digest = (status.image_id or "").partition("@")
            if digest.startswith("sha256:"):
                return digest
    return None


async def _deployment_part(
    api_client: client.ApiClient, namespace: str, release: str
) -> dict[str, Any]:
    try:
        deployment = await client.AppsV1Api(api_client).read_namespaced_deployment(
            release, namespace
        )
    except Exception as exc:
        return _not_found_or_unknown(exc)
    containers = deployment.spec.template.spec.containers or []
    main = next((c for c in containers if c.name == release), containers[0] if containers else None)
    part: dict[str, Any] = {"state": _PRESENT, "name": release}
    status = deployment.status
    part["replicas"] = status.replicas if status else None
    part["ready_replicas"] = status.ready_replicas if status else None
    if main is None or not main.image:
        return {**part, "image": None, "tag": None, "digest": None}
    part.update(
        image=main.image, **{k: v for k, v in parse_image(main.image).items() if k != "repository"}
    )
    if part["digest"] is None:
        selector = (deployment.spec.selector.match_labels or {}) if deployment.spec.selector else {}
        try:
            if selector:
                part["digest"] = await _pod_digest(
                    client.CoreV1Api(api_client), namespace, selector
                )
        except ApiException as exc:
            if exc.status == 401:
                raise
        except Exception:
            pass
    return part


async def _helm_chart_part(
    api_client: client.ApiClient, namespace: str, release: str
) -> dict[str, Any]:
    try:
        obj = await client.CustomObjectsApi(api_client).get_namespaced_custom_object(
            group=_HELM_CHART_GROUP,
            version=_HELM_CHART_VERSION,
            namespace=namespace,
            plural=_HELM_CHART_PLURAL,
            name=release,
        )
    except Exception as exc:
        return _not_found_or_unknown(exc)
    spec = obj.get("spec") or {}
    chart = spec.get("chart")
    return {
        "state": _PRESENT,
        "name": release,
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
    core = client.CoreV1Api(api_client)
    selector = f"owner=helm,name={release}"
    rows: list[dict[str, Any]] = []
    token: str | None = None
    incomplete = False
    try:
        for page in range(_SECRET_MAX_PAGES):
            kwargs: dict[str, Any] = {"label_selector": selector, "limit": _SECRET_PAGE_SIZE}
            if token:
                kwargs["_continue"] = token
            resp = await core.list_namespaced_secret(namespace, **kwargs)
            rows.extend(_revision_row(s) for s in resp.items or [])
            token = resp.metadata._continue if resp.metadata else None
            if not token:
                break
            incomplete = page == _SECRET_MAX_PAGES - 1
    except Exception as exc:
        if isinstance(exc, ApiException) and exc.status == 401:
            raise
        return _unknown(exc)
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
                "Helm release name. It is also the name of the Deployment and of the "
                "HelmChart object. Default: meho."
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
            "description": "name, image, tag, digest, replicas, ready_replicas.",
        },
        "helm_chart": {**_PART, "description": "name, chart, version, values_hash."},
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
        "release": "Optional. Default 'meho'.",
        "host": "Optional, for example '<meho-host>' or '<meho-host>:8443'.",
    },
    "output_shape": (
        "{namespace, release, deployment, helm_chart, helm_releases, meho?}. Each part "
        "has state 'present', 'not present' or 'unknown' (with error). 'not present' "
        "is normal: helm_chart exists only on RKE2 installs, helm_releases only on "
        "installs made with 'helm install'. meho.ready.db_ahead true means the "
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
            "Changes nothing. Reads the MEHO Deployment (image tag and digest), the HelmChart "
            "object (chart version, values hash), the Helm release Secret labels (revision, "
            "status) and, for a given host, /version and /ready. Missing is 'not present'."
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
