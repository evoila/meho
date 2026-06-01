# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Dangerous-class single-call write ops for :class:`KubernetesConnector`.

G3.14-T1 (#1403). The higher-blast-radius half of the write surface --
ops that create or destroy resources, or that carry credential material:

* ``k8s.apply`` -- server-side apply (SSA) over the dynamic client. The
  manifest is a single- or multi-document YAML/JSON; each doc's GVK is
  resolved against the cluster's discovery doc
  (``DynamicClient.resources.get``) and applied with
  ``field_manager="meho"``. ``dry_run="server"`` runs SSA with the
  API's ``?dryRun=All`` so the call mutates nothing and returns the
  would-be object -- the diff-preview the approval reviewer needs. The
  real apply uses ``dry_run="none"`` (the default).
* ``k8s.delete`` -- delete by kind/name. **v1 is scoped to
  pod / job / replicaset only**; namespace / PVC / PV are explicitly
  rejected (their blast radius -- cascading data loss -- is out of scope
  for the first write cut). ``propagation_policy`` and
  ``grace_period_seconds`` are explicit params, not defaulted silently.
* ``k8s.secret.create`` / ``k8s.job.create`` -- create credential /
  workload material. Both classify (via
  :func:`~meho_backplane.broadcast.events.classify_op`) as
  ``credential_write`` so the broadcast publisher collapses the event to
  aggregate-only ``{op_class, result_status}`` -- the Secret ``data`` /
  ``stringData`` (and a Job pod-template's inline ``env`` secrets) never
  reach the SSE stream or any Slack mirror. The ``OperationResult``
  returned to the caller still carries a redacted summary; the handlers
  here never echo the raw secret material back.

All ops are ``requires_approval=True``. This module holds only the
dangerous-class handlers + the delete dispatch table; the op-registration
rows + JSON-Schema parameter shapes for *all* write ops live in
:mod:`ops_write_meta`.

References
----------
* Parent task: G3.14-T1 (#1403); Initiative G3.14 (#1398).
* Server-side apply:
  https://kubernetes.io/docs/reference/using-api/server-side-apply/
* ``kubernetes_asyncio.dynamic.DynamicClient`` (35.0.1):
  https://github.com/tomplus/kubernetes_asyncio/blob/master/kubernetes_asyncio/dynamic/client.py
* Redaction op-class (reused, not reinvented):
  :data:`meho_backplane.broadcast.events._CREDENTIAL_WRITE_OPS`
  (shipped by #1401).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import yaml
from kubernetes_asyncio import client
from kubernetes_asyncio.dynamic import DynamicClient

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.kubernetes.connector import KubernetesConnector
    from meho_backplane.connectors.kubernetes.kubeconfig import KubernetesTargetLike

__all__ = [
    "DELETABLE_KINDS",
    "FIELD_MANAGER",
    "ApplyManifestError",
    "UndeletableKindError",
    "k8s_apply",
    "k8s_delete",
    "k8s_job_create",
    "k8s_secret_create",
    "secret_create_summary",
]


#: The field manager SSA records as the owner of the fields meho applies.
#: A stable, meho-specific manager keeps SSA conflict detection meaningful
#: -- a later ``kubectl apply`` by a human is a distinct manager and the
#: API server surfaces the ownership conflict rather than silently
#: clobbering meho-owned fields.
FIELD_MANAGER: str = "meho"


#: kind -> (api-attr, delete-method) for the v1 delete dispatch table.
#: Deliberately scoped to ephemeral / recreatable workload objects.
#: Namespace / PVC / PV are excluded: deleting them cascades to data
#: loss (every object in a namespace; the bound volume's data) which is
#: out of scope for the first write cut -- a later sub-task adds them
#: behind extra guard-rails.
DELETABLE_KINDS: dict[str, dict[str, str]] = {
    "pod": {"api": "CoreV1Api", "delete": "delete_namespaced_pod"},
    "job": {"api": "BatchV1Api", "delete": "delete_namespaced_job"},
    "replicaset": {"api": "AppsV1Api", "delete": "delete_namespaced_replica_set"},
}


class ApplyManifestError(ValueError):
    """The apply manifest could not be parsed or is missing required fields.

    Subclasses :class:`ValueError` so the dispatcher's
    ``connector_error`` envelope tags
    ``extras.exception_class="ApplyManifestError"`` and the operator
    sees the parse/validation reason rather than an opaque YAML error.
    """


class UndeletableKindError(ValueError):
    """The requested delete kind is outside the v1 pod/job/replicaset scope."""


def _parse_manifest(manifest: str) -> list[dict[str, Any]]:
    """Parse a single- or multi-document YAML/JSON manifest into doc dicts.

    ``yaml.safe_load_all`` handles both JSON (a subset of YAML) and
    multi-doc YAML separated by ``---``. Empty documents (a trailing
    ``---`` or blank stanza) are dropped. Each surviving doc must be a
    mapping carrying ``apiVersion`` + ``kind`` + ``metadata.name`` --
    SSA cannot address an object without them. Raises
    :class:`ApplyManifestError` on any structural problem.
    """
    try:
        docs = list(yaml.safe_load_all(manifest))
    except yaml.YAMLError as exc:
        raise ApplyManifestError(f"manifest is not valid YAML/JSON: {exc}") from exc

    parsed: list[dict[str, Any]] = []
    for idx, doc in enumerate(docs):
        if doc is None:
            continue
        if not isinstance(doc, dict):
            raise ApplyManifestError(f"manifest document {idx} is not a mapping")
        api_version = doc.get("apiVersion")
        kind = doc.get("kind")
        name = (doc.get("metadata") or {}).get("name")
        if not api_version or not kind:
            raise ApplyManifestError(f"manifest document {idx} is missing apiVersion/kind")
        if not name:
            raise ApplyManifestError(
                f"manifest document {idx} ({api_version}/{kind}) is missing metadata.name"
            )
        parsed.append(doc)
    if not parsed:
        raise ApplyManifestError("manifest contains no applyable documents")
    return parsed


async def _apply_one(
    dyn: DynamicClient,
    doc: dict[str, Any],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """Server-side-apply one manifest document, returning a flat summary.

    Resolves the GVK against the cluster's discovery doc, then calls
    ``server_side_apply`` with ``field_manager="meho"`` and, when
    *dry_run* is set, the API's ``dry_run="All"`` so nothing is
    persisted. The returned summary echoes the applied object's identity
    + the observed ``resourceVersion`` so a multi-doc apply reports
    per-document outcomes the reviewer can read.
    """
    api_version = doc["apiVersion"]
    kind = doc["kind"]
    metadata = doc.get("metadata") or {}
    name = metadata.get("name")
    namespace = metadata.get("namespace")

    resource = await dyn.resources.get(api_version=api_version, kind=kind)
    kwargs: dict[str, Any] = {"field_manager": FIELD_MANAGER}
    if dry_run:
        # The K8s API spells server dry-run ``dryRun=All``; the
        # operator-facing param is ``dry_run="server"`` (mapped by the
        # caller). force_conflicts is left unset -- a dry-run that hits
        # a field-ownership conflict is signal the reviewer wants.
        kwargs["dry_run"] = "All"
    applied = await dyn.server_side_apply(
        resource,
        body=doc,
        name=name,
        namespace=namespace,
        **kwargs,
    )
    applied_meta = getattr(applied, "metadata", None)
    return {
        "api_version": api_version,
        "kind": kind,
        "name": name,
        "namespace": namespace,
        "resource_version": getattr(applied_meta, "resourceVersion", None),
        "uid": getattr(applied_meta, "uid", None),
    }


async def k8s_apply(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.apply`` -- server-side apply with a dry-run preview.

    Parses the (multi-doc) ``manifest``, resolves each document's GVK via
    the dynamic client's discovery, and server-side-applies it under the
    ``meho`` field manager. ``dry_run="server"`` runs every document with
    the API's ``?dryRun=All`` so the call mutates nothing -- the returned
    per-document summary is the diff-preview surfaced to the approval
    reviewer before the real apply. ``dry_run="none"`` (default) persists.

    The dispatcher gates this op behind ``requires_approval=True`` for
    every principal kind, so a dry-run preview and the real apply are
    both reviewed -- the dry-run path is the cheap, side-effect-free
    rehearsal an agent or reviewer runs first.
    """
    manifest: str = params["manifest"]
    dry_run_mode: str = params.get("dry_run", "none")
    is_dry_run = dry_run_mode == "server"
    docs = _parse_manifest(manifest)

    api_client = await connector._get_api_client(target, operator)
    dyn = await DynamicClient(api_client)
    results = [await _apply_one(dyn, doc, dry_run=is_dry_run) for doc in docs]
    return {
        "dry_run": is_dry_run,
        "field_manager": FIELD_MANAGER,
        "applied": results,
        "total": len(results),
    }


async def k8s_delete(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.delete`` -- delete a pod/job/replicaset by name.

    v1 is scoped to the :data:`DELETABLE_KINDS` table; any other kind
    raises :class:`UndeletableKindError` (the schema's ``enum`` already
    rejects them, so this is defence-in-depth against a schema
    relaxation). ``propagation_policy`` (Foreground / Background /
    Orphan) and ``grace_period_seconds`` are forwarded explicitly so the
    cascade behaviour is never an implicit default -- e.g. deleting a Job
    with ``propagation_policy="Background"`` reaps its pods, ``Orphan``
    leaves them.
    """
    kind: str = params["kind"]
    name: str = params["name"]
    namespace: str = params["namespace"]
    row = DELETABLE_KINDS.get(kind.lower())
    if row is None:
        supported = ", ".join(sorted(DELETABLE_KINDS))
        raise UndeletableKindError(
            f"kind {kind!r} cannot be deleted in v1; supported kinds: {supported}"
        )

    api_client = await connector._get_api_client(target, operator)
    api = getattr(client, row["api"])(api_client)
    delete_method = getattr(api, row["delete"])
    kwargs: dict[str, Any] = {}
    if params.get("propagation_policy") is not None:
        kwargs["propagation_policy"] = params["propagation_policy"]
    if params.get("grace_period_seconds") is not None:
        kwargs["grace_period_seconds"] = int(params["grace_period_seconds"])
    await delete_method(name=name, namespace=namespace, **kwargs)
    return {
        "kind": kind.lower(),
        "name": name,
        "namespace": namespace,
        "propagation_policy": params.get("propagation_policy"),
        "grace_period_seconds": params.get("grace_period_seconds"),
        "deleted": True,
    }


def secret_create_summary(
    name: str, namespace: str, secret_type: str, data_keys: list[str]
) -> dict[str, Any]:
    """Build the no-value summary returned by ``k8s.secret.create``.

    Echoes the secret's identity + type + the *key names* it carries,
    but **never** the values. This is the response the caller's
    ``OperationResult`` wraps -- a defence-in-depth complement to the
    broadcast-layer ``credential_write`` redaction: even the
    handler-produced result is value-free, so a misconfigured downstream
    consumer that logs the result still can't leak the secret.
    """
    return {
        "name": name,
        "namespace": namespace,
        "type": secret_type,
        "data_keys": sorted(data_keys),
        "created": True,
    }


async def k8s_secret_create(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.secret.create`` -- create an Opaque (or typed) Secret.

    Accepts ``string_data`` (plaintext, base64-encoded by the API server)
    and/or ``data`` (already base64-encoded). The values are written to
    the cluster but never echoed back: the response is
    :func:`secret_create_summary` (key names only). The op classifies
    ``credential_write`` so the broadcast event collapses to
    aggregate-only -- the values in ``params`` never reach the feed.
    """
    name: str = params["name"]
    namespace: str = params["namespace"]
    secret_type: str = params.get("type", "Opaque")
    string_data: dict[str, str] = dict(params.get("string_data") or {})
    data: dict[str, str] = dict(params.get("data") or {})

    api_client = await connector._get_api_client(target, operator)
    core_v1 = client.CoreV1Api(api_client)
    body = client.V1Secret(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace),
        type=secret_type,
        string_data=string_data or None,
        data=data or None,
    )
    await core_v1.create_namespaced_secret(namespace=namespace, body=body)
    return secret_create_summary(name, namespace, secret_type, list(string_data) + list(data))


async def k8s_job_create(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.job.create`` -- create a batch Job from a manifest.

    The Job is supplied as a manifest dict (``spec`` body) rather than
    decomposed params -- a Job's pod template is too rich to flatten.
    The pod template can carry inline ``env`` secret material, so the op
    classifies ``credential_write`` and the broadcast collapses to
    aggregate-only. The response echoes the Job's identity only, never
    the template body.
    """
    name: str = params["name"]
    namespace: str = params["namespace"]
    job_spec: dict[str, Any] = params["spec"]

    api_client = await connector._get_api_client(target, operator)
    batch_v1 = client.BatchV1Api(api_client)
    body: dict[str, Any] = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": namespace},
        "spec": job_spec,
    }
    # kubernetes_asyncio's typed ``create_namespaced_job`` serialises a
    # raw dict body verbatim at runtime (``sanitize_for_serialization``
    # passes dicts through); the typed stub insists on ``V1Job`` so cast
    # to keep the env-secret-bearing spec opaque without a lossy
    # field-by-field reconstruction into the model.
    await batch_v1.create_namespaced_job(namespace=namespace, body=cast("Any", body))
    return {"name": name, "namespace": namespace, "created": True}
