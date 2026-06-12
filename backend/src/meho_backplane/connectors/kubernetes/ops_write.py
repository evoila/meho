# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Caution-class single-call write ops for :class:`KubernetesConnector`.

G3.14-T1 (#1403) of Initiative #1398. The connector shipped read-only;
this module lands the lower-blast-radius write surface (everything that
is reversible or idempotent):

* ``k8s.scale`` -- set a Deployment's replica count via the ``/scale``
  subresource (``AppsV1Api.patch_namespaced_deployment_scale``). The
  result carries before/after replica counts so the approval reviewer
  and the audit row both record the delta.
* ``k8s.rollout.restart`` -- stamp the
  ``kubectl.kubernetes.io/restartedAt`` pod-template annotation on a
  Deployment, which the Deployment controller treats as a template
  change and rolls every pod. Mirrors ``kubectl rollout restart``.
* ``k8s.namespace.create`` -- create-or-ignore-409. Idempotent: a
  re-create against an existing namespace returns ``created=False``
  rather than erroring, so the op is safe to retry.
* ``k8s.annotate`` / ``k8s.label`` -- generic strategic-merge patch of
  ``metadata.annotations`` / ``metadata.labels`` over a kind→method
  dispatch table. A ``null`` value removes the key (kubectl's
  ``key-`` syntax). Relabeling a Service's selector-matched workload
  can change traffic routing -- the op stays ``caution`` for that
  reason, documented in the llm_instructions.
* ``k8s.cordon`` -- toggle a Node's ``spec.unschedulable`` flag via
  ``CoreV1Api.patch_node``. Reversible (``uncordon=true`` flips it
  back). No eviction -- that is ``k8s.drain``, deferred.

All ops are ``requires_approval=True``; once #1401's human-queue routing
is live a human caller's dispatch is parked for approval rather than
denied. The dangerous-class writes (apply / delete / secret.create /
job.create) live in :mod:`ops_write_dangerous`. This module holds only
the caution-class handlers + their kind dispatch table; the declarative
op-registration rows + JSON-Schema parameter shapes for *all* write ops
live in :mod:`ops_write_meta` (split out to keep each file under the
600-line code-quality cap and to read the operator-facing surface in one
place).

References
----------
* Parent task: G3.14-T1 (#1403); Initiative G3.14 (#1398).
* ``kubernetes_asyncio.AppsV1Api`` /
  ``kubernetes_asyncio.CoreV1Api`` (35.0.1):
  https://github.com/tomplus/kubernetes_asyncio/tree/master/kubernetes_asyncio/docs
* Deployment ``/scale`` subresource:
  https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/deployment-v1/#DeploymentSpec
* Rollout-restart annotation contract (kubectl parity):
  https://kubernetes.io/docs/reference/generated/kubectl/kubectl-commands#rollout
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from kubernetes_asyncio import client

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.kubernetes.connector import KubernetesConnector
    from meho_backplane.connectors.kubernetes.kubeconfig import KubernetesTargetLike

__all__ = [
    "ANNOTATABLE_KINDS",
    "RESTART_ANNOTATION",
    "UnsupportedKindError",
    "k8s_annotate",
    "k8s_cordon",
    "k8s_label",
    "k8s_namespace_create",
    "k8s_rollout_restart",
    "k8s_scale",
]


#: The pod-template annotation ``kubectl rollout restart`` stamps to
#: trigger a rolling restart. Writing a fresh timestamp here is a
#: template mutation the Deployment controller observes and rolls.
RESTART_ANNOTATION: str = "kubectl.kubernetes.io/restartedAt"


#: kind -> (api-attr, read-method, patch-method) for the generic
#: annotate/label patch surface. Kept deliberately small in v1 -- the
#: kinds an operator routinely annotates/labels (Deployment / Pod /
#: Service / Namespace / Node). ``api_attr`` is the
#: :class:`KubernetesConnector`-resolved API object; the read/patch
#: method names are looked up on it via ``getattr`` so a typo surfaces
#: as a structured error rather than an opaque ``AttributeError``.
ANNOTATABLE_KINDS: dict[str, dict[str, str]] = {
    "deployment": {
        "api": "AppsV1Api",
        "patch": "patch_namespaced_deployment",
        "namespaced": "true",
    },
    "pod": {
        "api": "CoreV1Api",
        "patch": "patch_namespaced_pod",
        "namespaced": "true",
    },
    "service": {
        "api": "CoreV1Api",
        "patch": "patch_namespaced_service",
        "namespaced": "true",
    },
    "namespace": {
        "api": "CoreV1Api",
        "patch": "patch_namespace",
        "namespaced": "false",
    },
    "node": {
        "api": "CoreV1Api",
        "patch": "patch_node",
        "namespaced": "false",
    },
}


class UnsupportedKindError(ValueError):
    """The requested kind is not in the v1 annotate/label dispatch table.

    Subclasses :class:`ValueError` so the dispatcher's
    ``connector_error`` envelope carries
    ``extras.exception_class="UnsupportedKindError"`` and the operator
    sees the supported-kind list rather than an opaque key error.
    """


def _metadata_patch_body(field: str, entries: dict[str, str | None]) -> dict[str, Any]:
    """Build the strategic-merge patch body for a metadata map field.

    *field* is ``"annotations"`` or ``"labels"``. A ``None`` value in
    *entries* deletes the key (the strategic-merge semantics for a map
    entry set to ``null``), mirroring kubectl's ``key-`` removal syntax.
    Non-null values are coerced to ``str`` -- the K8s API rejects
    non-string annotation/label values, and surfacing a clean coercion
    here beats a 422 from the server.
    """
    coerced: dict[str, str | None] = {
        k: (None if v is None else str(v)) for k, v in entries.items()
    }
    return {"metadata": {field: coerced}}


async def k8s_scale(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.scale``.

    Reads the Deployment's current ``/scale`` subresource to capture the
    before-count, then patches it to the requested ``replicas``. The
    response carries ``{name, namespace, replicas_before, replicas_after}``
    so the audit row and the approval reviewer both see the delta. Uses
    the dedicated ``/scale`` subresource (not a spec patch) so the op
    works identically against a Deployment whose replica count is
    HPA-managed -- the scale subresource is the canonical write surface.
    """
    name: str = params["name"]
    namespace: str = params["namespace"]
    replicas: int = int(params["replicas"])
    api_client = await connector._get_api_client(target, operator)
    apps_v1 = client.AppsV1Api(api_client)
    current = await apps_v1.read_namespaced_deployment_scale(name=name, namespace=namespace)
    before = (
        int(current.spec.replicas)
        if current.spec is not None and current.spec.replicas is not None
        else 0
    )
    patched = await apps_v1.patch_namespaced_deployment_scale(
        name=name,
        namespace=namespace,
        body={"spec": {"replicas": replicas}},
    )
    after = (
        int(patched.spec.replicas)
        if patched.spec is not None and patched.spec.replicas is not None
        else replicas
    )
    return {
        "name": name,
        "namespace": namespace,
        "replicas_before": before,
        "replicas_after": after,
    }


async def k8s_rollout_restart(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.rollout.restart``.

    Stamps a fresh RFC3339 timestamp into the Deployment's pod-template
    ``kubectl.kubernetes.io/restartedAt`` annotation. The Deployment
    controller treats the template change as a new revision and rolls
    every pod under the active rollout strategy -- exactly what
    ``kubectl rollout restart deployment/<name>`` does. The patch
    targets ``spec.template.metadata.annotations`` (the pod template),
    not the Deployment's own metadata, so the controller observes it.
    """
    name: str = params["name"]
    namespace: str = params["namespace"]
    api_client = await connector._get_api_client(target, operator)
    apps_v1 = client.AppsV1Api(api_client)
    restarted_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {
        "spec": {
            "template": {
                "metadata": {"annotations": {RESTART_ANNOTATION: restarted_at}},
            }
        }
    }
    await apps_v1.patch_namespaced_deployment(name=name, namespace=namespace, body=body)
    return {
        "name": name,
        "namespace": namespace,
        "restarted_at": restarted_at,
    }


async def k8s_namespace_create(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.namespace.create`` -- idempotent create-or-ignore-409.

    A 409 Conflict (namespace already exists) is swallowed and reported
    as ``created=False`` so the op is safe to retry and safe to run as
    a no-op precondition before a deploy. Any other API error
    propagates to the dispatcher's ``connector_error`` envelope.
    """
    from kubernetes_asyncio.client.exceptions import ApiException

    name: str = params["name"]
    api_client = await connector._get_api_client(target, operator)
    core_v1 = client.CoreV1Api(api_client)
    body = client.V1Namespace(metadata=client.V1ObjectMeta(name=name))
    try:
        await core_v1.create_namespace(body=body)
    except ApiException as exc:
        if exc.status == 409:
            return {"name": name, "created": False, "already_existed": True}
        raise
    return {"name": name, "created": True, "already_existed": False}


def _resolve_annotatable(kind: str) -> dict[str, str]:
    """Look up the dispatch-table row for *kind* or raise.

    Normalises to lower-case so ``Deployment`` and ``deployment`` both
    resolve. Raises :class:`UnsupportedKindError` listing the supported
    kinds when the kind is outside the v1 table.
    """
    row = ANNOTATABLE_KINDS.get(kind.lower())
    if row is None:
        supported = ", ".join(sorted(ANNOTATABLE_KINDS))
        raise UnsupportedKindError(
            f"kind {kind!r} is not annotatable/labelable in v1; supported kinds: {supported}"
        )
    return row


async def _patch_metadata_map(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
    *,
    field: str,
) -> dict[str, Any]:
    """Shared body for :func:`k8s_annotate` and :func:`k8s_label`.

    *field* selects ``"annotations"`` or ``"labels"``. Resolves the
    kind→API/method row, builds the strategic-merge patch, and invokes
    the kind-appropriate namespaced / cluster-scoped patch method. The
    response echoes the applied entries so the audit row records exactly
    which keys changed.
    """
    kind: str = params["kind"]
    name: str = params["name"]
    namespace: str | None = params.get("namespace")
    entries: dict[str, str | None] = dict(params[field])
    row = _resolve_annotatable(kind)

    api_client = await connector._get_api_client(target, operator)
    api = getattr(client, row["api"])(api_client)
    patch_method = getattr(api, row["patch"])
    body = _metadata_patch_body(field, entries)

    if row["namespaced"] == "true":
        if not namespace:
            raise ValueError(f"namespace is required to {field[:-1]} a {kind}")
        await patch_method(name=name, namespace=namespace, body=body)
    else:
        await patch_method(name=name, body=body)

    return {
        "kind": kind.lower(),
        "name": name,
        "namespace": namespace if row["namespaced"] == "true" else None,
        field: entries,
    }


async def k8s_annotate(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.annotate`` -- strategic-merge patch of annotations."""
    return await _patch_metadata_map(connector, target, operator, params, field="annotations")


async def k8s_label(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.label`` -- strategic-merge patch of labels.

    Relabeling a workload that a Service selects on (or a Deployment's
    own selector-matched pods) can silently re-route traffic or orphan
    pods -- the op stays ``caution`` for that reason.
    """
    return await _patch_metadata_map(connector, target, operator, params, field="labels")


async def k8s_cordon(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.cordon`` -- toggle a Node's ``spec.unschedulable``.

    ``uncordon=true`` flips the flag back to schedulable. Reversible and
    eviction-free: cordon only stops *new* pods from scheduling; running
    pods are untouched (draining them is ``k8s.drain``, deferred). The
    response carries the resulting ``unschedulable`` state so the audit
    row records the intent unambiguously.
    """
    name: str = params["name"]
    uncordon = bool(params.get("uncordon", False))
    unschedulable = not uncordon
    api_client = await connector._get_api_client(target, operator)
    core_v1 = client.CoreV1Api(api_client)
    await core_v1.patch_node(name=name, body={"spec": {"unschedulable": unschedulable}})
    return {"name": name, "unschedulable": unschedulable, "cordoned": unschedulable}
