# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Free one object that is stuck while it is being deleted: ``k8s.finalizer.remove``.

A finalizer is a hold. Kubernetes does not remove an object that has a
deletion time until every finalizer is gone. When the controller's
clean-up can never succeed, the object stays "being deleted" forever. The
usual fix was ``kubectl patch``, outside the approval queue and the audit
log. This op closes that gap.

``k8s.finalizer.remove`` (``destructive``)
    Removes exactly one named finalizer from exactly one named object. The
    caller first runs ``preview_operation``, then ``call_operation`` with
    the returned ``preview_hash``; a person's call waits for a second
    person, an agent's call is refused. The op works on any kind: a core
    kind (``group`` empty) or a custom resource. It refuses when:

    * the object does not exist,
    * the object is not being deleted (it has no deletion time),
    * the object does not have the named finalizer,
    * the list on the object differs from the list on the approval card
      the approver saw (the list changed while the request waited), or
      from ``expected_finalizers`` when that is given.

    The write is a JSON Patch with a ``test`` step on the whole finalizer
    list. If the list changes between the read and the write, the API
    server rejects the patch and nothing is overwritten. Other finalizers
    stay as they are. The op takes one name, never a label or a selector.

    The preview builder runs the same checks, so a refusal fails the
    preview and nothing waits for approval. The approval card shows the
    object, its deletion time and its finalizers, and says that the
    controller's clean-up is skipped.

A core ``secrets`` object is refused: the API answers with the whole
object, so its values would pass through the wire record.

References
----------
* Finalizers:
  https://kubernetes.io/docs/concepts/overview/working-with-objects/finalizers/
* JSON Patch (RFC 6902): ``test`` fails the whole patch when the value
  differs. The API server answers 422.
* ``kubernetes_asyncio`` 36.1.0 ``ApiClient.call_api(resource_path, method,
  path_params, query_params, header_params, body, ...)``; path parameters
  are percent-encoded, so ``/`` in a name cannot reach another path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kubernetes_asyncio.client.exceptions import ApiException

from meho_backplane.connectors.kubernetes.ops import KubernetesOp
from meho_backplane.operations._preview import (
    PreviewContext,
    approved_preview,
    register_preview_builder,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.kubernetes.connector import KubernetesConnector
    from meho_backplane.connectors.kubernetes.kubeconfig import KubernetesTargetLike

__all__ = [
    "FINALIZER_OPS",
    "OPS",
    "KubernetesFinalizerError",
    "k8s_finalizer_remove",
]

_OP_ID = "k8s.finalizer.remove"
_PATCH_CONTENT_TYPE = "application/json-patch+json"
_RESPONSE_TYPES = {200: "object", 401: None}


class KubernetesFinalizerError(ValueError):
    """The finalizer cannot be removed; nothing was written.

    Subclasses :class:`ValueError` so the dispatcher's ``connector_error``
    envelope tags the class. The message says what to do next; the same
    text is ``remediation``.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.remediation = message


def _label(params: dict[str, Any]) -> str:
    group = params["group"]
    namespace = params.get("namespace")
    where = f"{namespace}/" if namespace else ""
    kind = f"{params['plural']}.{group}" if group else params["plural"]
    return f"{kind} {where}{params['name']}"


def _resource_path(params: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """The URL template and path parameters of the named object."""
    group = params["group"]
    path_params = {"version": params["version"], "plural": params["plural"], "name": params["name"]}
    root = "/api/{version}"
    if group:
        root = "/apis/{group}/{version}"
        path_params["group"] = group
    if params.get("namespace"):
        path_params["namespace"] = params["namespace"]
        return f"{root}/namespaces/{{namespace}}/{{plural}}/{{name}}", path_params
    return f"{root}/{{plural}}/{{name}}", path_params


async def _call(
    api_client: Any,
    method: str,
    params: dict[str, Any],
    *,
    body: Any = None,
    content_type: str | None = None,
) -> dict[str, Any]:
    resource_path, path_params = _resource_path(params)
    headers = {"Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = content_type
    result: dict[str, Any] = await api_client.call_api(
        resource_path,
        method,
        path_params=path_params,
        query_params=[],
        header_params=headers,
        body=body,
        response_types_map=_RESPONSE_TYPES,
        auth_settings=["BearerToken"],
        _return_http_data_only=True,
    )
    return result


def _refuse_secrets(params: dict[str, Any]) -> None:
    if params["group"] == "" and params["plural"].lower() == "secrets":
        raise KubernetesFinalizerError(
            "k8s.finalizer.remove does not work on core secrets: the API answer "
            "carries the secret values. Use k8s.secret.delete for a Secret."
        )


def _approved_finalizers(label: str) -> list[str] | None:
    """The finalizer list on the approval card, during the approved run of this op.

    ``None`` when the call is not the approved run of a request. An
    approved run whose card has no finalizer list is refused: MEHO cannot
    tell whether the list changed while the request waited.
    """
    preview = approved_preview(_OP_ID)
    if preview is None:
        return None
    obj = (preview.get("blast_radius") or {}).get("object") or {}
    finalizers = obj.get("finalizers")
    if not isinstance(finalizers, list):
        raise KubernetesFinalizerError(
            f"The approval card of {label} shows no finalizer list, so MEHO cannot "
            "check that the list is unchanged. Nothing was changed. Send a new request."
        )
    return [str(f) for f in finalizers]


async def read_stuck_object(api_client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Read the object and check it can be freed; return its metadata view.

    Raises :class:`KubernetesFinalizerError` for a missing object, an
    object that is not being deleted, a finalizer it does not have, or a
    list that differs from ``expected_finalizers`` or from the approval
    card of the approved run. Returns
    ``{kind, deletion_timestamp, finalizers}``.
    """
    _refuse_secrets(params)
    label = _label(params)
    try:
        obj = await _call(api_client, "GET", params)
    except ApiException as exc:
        if exc.status == 404:
            raise KubernetesFinalizerError(f"{label} does not exist; nothing was changed.") from exc
        raise
    metadata = obj.get("metadata") or {}
    deletion = metadata.get("deletionTimestamp")
    if not deletion:
        raise KubernetesFinalizerError(
            f"{label} is not being deleted (it has no deletion time). Delete it "
            "first; remove a finalizer only when the delete is stuck."
        )
    finalizers = [str(f) for f in (metadata.get("finalizers") or [])]
    if params["finalizer"] not in finalizers:
        raise KubernetesFinalizerError(
            f"{label} has no finalizer {params['finalizer']!r}. Its finalizers: "
            f"{finalizers or 'none'}."
        )
    for expected in (params.get("expected_finalizers"), _approved_finalizers(label)):
        if expected is not None and list(expected) != finalizers:
            raise KubernetesFinalizerError(
                f"The finalizers of {label} changed since you looked: now {finalizers}. "
                "Run the preview again and check the list."
            )
    return {
        "kind": obj.get("kind"),
        "deletion_timestamp": str(deletion),
        "finalizers": finalizers,
    }


async def k8s_finalizer_remove(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.finalizer.remove``: remove one finalizer from one object."""
    api_client = await connector._get_api_client(target, operator)
    seen = await read_stuck_object(api_client, params)
    current: list[str] = seen["finalizers"]
    remaining = [f for f in current if f != params["finalizer"]]
    patch = [
        {"op": "test", "path": "/metadata/finalizers", "value": current},
        {"op": "replace", "path": "/metadata/finalizers", "value": remaining},
    ]
    label = _label(params)
    try:
        await _call(api_client, "PATCH", params, body=patch, content_type=_PATCH_CONTENT_TYPE)
    except ApiException as exc:
        if exc.status == 404:
            raise KubernetesFinalizerError(
                f"{label} is gone already; nothing was changed."
            ) from exc
        if exc.status in (409, 422):
            raise KubernetesFinalizerError(
                f"The finalizers of {label} changed while the call ran; nothing was "
                "written. Run the preview again."
            ) from exc
        raise
    return {
        "group": params["group"],
        "version": params["version"],
        "plural": params["plural"],
        "namespace": params.get("namespace"),
        "name": params["name"],
        "finalizer": params["finalizer"],
        "removed": True,
        "remaining_finalizers": remaining,
    }


async def _finalizer_remove_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``k8s.finalizer.remove``: the object, its deletion time and finalizers.

    Returns ``None`` without a connector or target; the destructive gate
    then refuses to park (fail-closed). A refusal raises, so nothing waits
    for approval.
    """
    from meho_backplane.connectors.kubernetes.connector import KubernetesConnector

    if not isinstance(ctx.connector_instance, KubernetesConnector) or ctx.target is None:
        return None
    params = ctx.params
    api_client = await ctx.connector_instance._get_api_client(ctx.target, ctx.operator)
    seen = await read_stuck_object(api_client, params)
    obj = {
        "kind": seen["kind"] or params["plural"],
        "group": params["group"],
        "version": params["version"],
        "plural": params["plural"],
        "namespace": params.get("namespace"),
        "name": params["name"],
        "deletion_timestamp": seen["deletion_timestamp"],
        "finalizers": seen["finalizers"],
    }
    return {
        "resource": "finalizer",
        "finalizer": params["finalizer"],
        "warning": (
            "The controller that owns this finalizer will not run its clean-up. "
            "Anything it would have removed stays behind."
        ),
        "blast_radius": {
            "object": obj,
            "children": [],
            "irreversibility": "permanent",
            "recovery": "none: the finalizer is gone and the object can finish deleting",
        },
    }


register_preview_builder(_OP_ID, _finalizer_remove_preview)

_FINALIZER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "group": {
            "type": "string",
            "description": "API group; empty string for a core kind.",
        },
        "version": {"type": "string", "minLength": 1, "description": "API version, e.g. v1."},
        "plural": {
            "type": "string",
            "minLength": 1,
            "description": "Plural resource name, as k8s.crd.list shows it.",
        },
        "namespace": {
            "type": "string",
            "minLength": 1,
            "description": "Namespace; leave out for a cluster-scoped kind.",
        },
        "name": {"type": "string", "minLength": 1, "description": "Exact object name."},
        "finalizer": {
            "type": "string",
            "minLength": 1,
            "description": "Exact name of the one finalizer to remove.",
        },
        "expected_finalizers": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "The finalizer list the preview showed. When given, the call fails "
                "if the list on the object is different now."
            ),
        },
    },
    "required": ["group", "version", "plural", "name", "finalizer"],
    "additionalProperties": False,
    "examples": [
        {
            "group": "<api-group>",
            "version": "<version>",
            "plural": "<plural>",
            "namespace": "<namespace>",
            "name": "<object-name>",
            "finalizer": "<finalizer-name>",
        }
    ],
}

_FINALIZER_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "group": {"type": "string"},
        "version": {"type": "string"},
        "plural": {"type": "string"},
        "namespace": {"type": ["string", "null"]},
        "name": {"type": "string"},
        "finalizer": {"type": "string"},
        "removed": {"type": "boolean"},
        "remaining_finalizers": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["group", "version", "plural", "name", "finalizer", "removed"],
    "additionalProperties": False,
}

_FINALIZER_LLM: dict[str, Any] = {
    "when_to_call": (
        "Free one object that stays 'being deleted' because a controller's "
        "clean-up can never finish. Destructive: call preview_operation first, "
        "then call_operation with the preview_hash it returned; a person's call "
        "waits for a second person, an agent's call is refused."
    ),
    "preconditions": (
        "The object already has a deletion time and really has the named "
        "finalizer; otherwise the preview fails and nothing waits for approval. "
        "The controller's clean-up is skipped, so check that nothing outside the "
        "cluster needs it."
    ),
    "parameter_hints": {
        "group": "Empty string for a core kind.",
        "plural": "Plural resource name (k8s.crd.list shows it).",
        "finalizer": "Exact name, as in metadata.finalizers (k8s.cr.info shows it).",
        "expected_finalizers": "Pass the list the preview showed to catch a change.",
    },
    "output_shape": "{group, version, plural, namespace, name, finalizer, removed, "
    "remaining_finalizers}.",
    "next_step": (
        "If other finalizers remain, the object stays 'being deleted'. Run k8s.cr.info "
        "to check it is gone. To delete an object that is not yet being deleted, "
        "use k8s.delete or k8s.apply."
    ),
}

FINALIZER_OPS: tuple[KubernetesOp, ...] = (
    KubernetesOp(
        op_id=_OP_ID,
        handler_attr="k8s_finalizer_remove",
        summary="Remove one finalizer from one object that is stuck while being deleted.",
        description=(
            "Removes exactly the named finalizer from one named object that is "
            "already being deleted. Refuses any other object. Destructive: preview "
            "first, then call with the preview_hash; a second person must approve. "
            "The controller's clean-up is skipped."
        ),
        parameter_schema=_FINALIZER_SCHEMA,
        response_schema=_FINALIZER_RESPONSE,
        group_key="config",
        tags=("write", "finalizer", "delete", "destructive"),
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=_FINALIZER_LLM,
    ),
)

#: Collected by :func:`~meho_backplane.connectors.kubernetes.ops.collect_ops`.
OPS: tuple[KubernetesOp, ...] = FINALIZER_OPS
