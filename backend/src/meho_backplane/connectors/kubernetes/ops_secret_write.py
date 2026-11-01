# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Change or delete an existing Kubernetes Secret: ``k8s.secret.update`` / ``k8s.secret.delete``.

``k8s.secret.create`` only creates. A credential change (a new token or
password in a Secret a workload already reads) and the removal of a Secret
had no governed path, so they needed ``kubectl`` outside MEHO. These two
ops close that gap. Both wait for approval and write an audit row, and
neither shows a Secret value anywhere.

``k8s.secret.update`` (``dangerous``)
    Changes the values of an existing Secret. ``mode: patch`` (the default)
    sets the given keys and keeps every other key
    (``CoreV1Api.patch_namespaced_secret``, a strategic merge patch).
    ``mode: replace`` leaves exactly the given keys: the handler reads the
    Secret and sends it back with the new maps through
    ``CoreV1Api.replace_namespaced_secret``. The read ``resourceVersion``
    rides along, so a change by someone else between the read and the
    write makes the API server answer 409 and nothing is lost without
    notice. The op never creates a Secret, keeps the type, labels and
    annotations as they are, and refuses an ``immutable`` Secret. It takes
    the same value inputs as ``k8s.secret.create``; the ``*_secret_ref``
    inputs are resolved through the credential-backend seam
    (:func:`~meho_backplane.connectors.kubernetes.ops_write_dangerous._resolve_secret_refs`),
    so a Vault-resident value never passes through the caller. The op is
    pinned ``credential_write``
    (:data:`~meho_backplane.broadcast.events._CREDENTIAL_WRITE_OPS`): while
    it waits for approval its params live in the approval hand-off store,
    not on the approval row, and the broadcast collapses to aggregate-only.

``k8s.secret.delete`` (``destructive``)
    Deletes one Secret (``CoreV1Api.delete_namespaced_secret``). The
    destructive tier means: the caller first runs ``preview_operation``,
    then ``call_operation`` with the returned ``preview_hash``; a person's
    call waits for a second person; an agent's call is refused. The preview
    builder in :mod:`ops_secret_write_preview` reads the Secret and lists
    the workloads in the namespace that use it (the ``blast_radius`` block
    the approver reads). A missing Secret fails the preview, so nothing
    waits for approval.

The handlers return key names only, never a value. Errors name the Secret,
never a value. ``k8s.secret.update`` sends a secret, so the dispatcher
withholds the message of a raised error from the result (#4854). Each error
class here therefore also carries its text as ``remediation``: that field
is MEHO's own text, names no value, and stays in the result, the audit row
and the stored approval result. So the caller still reads what to do next.

References
----------
* Secret API:
  https://kubernetes.io/docs/reference/kubernetes-api/config-and-storage-resources/secret-v1/
* Immutable Secrets:
  https://kubernetes.io/docs/concepts/configuration/secret/#secret-immutable
* ``kubernetes_asyncio`` 36.1.0 ``CoreV1Api``: ``read_namespaced_secret(name,
  namespace)``, ``patch_namespaced_secret(name, namespace, body)`` (a dict
  body is sent as ``application/strategic-merge-patch+json``),
  ``replace_namespaced_secret(name, namespace, body)``,
  ``delete_namespaced_secret(name, namespace)``. A non-2xx answer raises
  ``ApiException`` with ``status``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kubernetes_asyncio import client
from kubernetes_asyncio.client.exceptions import ApiException

from meho_backplane.connectors._shared.vault_creds import DEFAULT_KV_MOUNT
from meho_backplane.connectors.base import ConnectorUpstreamError
from meho_backplane.connectors.kubernetes.ops import KubernetesOp
from meho_backplane.connectors.kubernetes.ops_write_dangerous import _resolve_secret_refs

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.kubernetes.connector import KubernetesConnector
    from meho_backplane.connectors.kubernetes.kubeconfig import KubernetesTargetLike

__all__ = [
    "SECRET_WRITE_OPS",
    "KubernetesSecretConflictError",
    "KubernetesSecretImmutableError",
    "KubernetesSecretNotFoundError",
    "k8s_secret_delete",
    "k8s_secret_update",
    "read_secret_or_raise",
    "secret_key_names",
]

_OP_UPDATE = "k8s.secret.update"
_OP_DELETE = "k8s.secret.delete"


class KubernetesSecretNotFoundError(ValueError):
    """The named Secret does not exist.

    Subclasses :class:`ValueError` so the dispatcher's ``connector_error``
    envelope tags ``extras.exception_class="KubernetesSecretNotFoundError"``.
    The message names the Secret and the op that creates one. The same text
    is ``remediation``, so it reaches the caller when the message is withheld.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.remediation = message


class KubernetesSecretImmutableError(ValueError):
    """The Secret has ``immutable: true``; Kubernetes refuses every change to it.

    The message says to delete the Secret and create it again. The same text
    is ``remediation``, so it reaches the caller when the message is withheld.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.remediation = message


class KubernetesSecretConflictError(ConnectorUpstreamError):
    """The Secret changed between the read and the write (HTTP 409).

    Raised only in ``replace`` mode. Nothing was written; the caller reads
    the Secret again and runs the op again. The message says so; the same
    text is ``remediation``, so it reaches the caller when the message and
    the API server's text are withheld.
    """

    def __init__(self, message: str, *, http_status: int, upstream_message: str | None) -> None:
        super().__init__(message, http_status=http_status, upstream_message=upstream_message)
        self.remediation = message


def secret_key_names(secret: Any) -> list[str]:
    """Sorted key names of ``secret.data`` and ``secret.string_data``; never a value."""
    keys: set[str] = set()
    for attr in ("data", "string_data"):
        value = getattr(secret, attr, None)
        if isinstance(value, dict):
            keys.update(str(key) for key in value)
    return sorted(keys)


async def read_secret_or_raise(core_v1: Any, *, name: str, namespace: str) -> Any:
    """Read the Secret; a 404 becomes :class:`KubernetesSecretNotFoundError`."""
    try:
        return await core_v1.read_namespaced_secret(name=name, namespace=namespace)
    except ApiException as exc:
        if exc.status == 404:
            raise KubernetesSecretNotFoundError(
                f"Secret {namespace}/{name} does not exist. {_OP_UPDATE} and "
                f"{_OP_DELETE} only change an existing Secret; use "
                f"k8s.secret.create to create it."
            ) from exc
        raise


async def _resolve_value_inputs(
    operator: Operator, target: KubernetesTargetLike, params: dict[str, Any]
) -> tuple[dict[str, str], dict[str, str]]:
    """Return ``(string_data, data)`` with the ``*_secret_ref`` maps resolved and merged.

    Same inputs and same merge rule as ``k8s.secret.create``: a ref wins a
    key collision with the inline map. Only the ref is in the params; the
    value is read here, in-process.
    """
    string_data: dict[str, str] = dict(params.get("string_data") or {})
    data: dict[str, str] = dict(params.get("data") or {})
    for inline, ref_field, mount_field in (
        (string_data, "string_data_secret_ref", "string_data_secret_mount"),
        (data, "data_secret_ref", "data_secret_mount"),
    ):
        refs = params.get(ref_field)
        if refs:
            inline.update(
                await _resolve_secret_refs(
                    operator,
                    target,
                    dict(refs),
                    mount=str(params.get(mount_field) or DEFAULT_KV_MOUNT).strip(),
                    param=ref_field,
                    op_id=_OP_UPDATE,
                )
            )
    return string_data, data


async def k8s_secret_update(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.secret.update``: change the values of an existing Secret.

    Reads the Secret first (a missing one raises
    :class:`KubernetesSecretNotFoundError`, an immutable one
    :class:`KubernetesSecretImmutableError`). ``patch`` sends only the
    given keys as a strategic merge patch, so every other key stays.
    ``replace`` sends the read object back with the new maps, so the
    Secret ends with exactly the given keys and the type, labels and
    annotations it had; its ``resourceVersion`` makes the write fail with
    409 (:class:`KubernetesSecretConflictError`) when someone changed the
    Secret in between. The result carries key names only.
    """
    name: str = params["name"]
    namespace: str = params["namespace"]
    mode: str = params.get("mode", "patch")
    string_data, data = await _resolve_value_inputs(operator, target, params)
    given_keys = sorted(set(string_data) | set(data))

    api_client = await connector._get_api_client(target, operator)
    core_v1 = client.CoreV1Api(api_client)
    existing = await read_secret_or_raise(core_v1, name=name, namespace=namespace)
    if getattr(existing, "immutable", None):
        raise KubernetesSecretImmutableError(
            f"Secret {namespace}/{name} is immutable, so its values cannot change. "
            f"Delete it with {_OP_DELETE} and create it again with k8s.secret.create."
        )
    existing_keys = secret_key_names(existing)
    secret_type = existing.type or "Opaque"

    if mode == "replace":
        existing.string_data = string_data or None
        existing.data = data or None
        try:
            await core_v1.replace_namespaced_secret(name=name, namespace=namespace, body=existing)
        except ApiException as exc:
            if exc.status == 409:
                raise KubernetesSecretConflictError(
                    f"Secret {namespace}/{name} changed between the read and the write; "
                    f"nothing was written. Run {_OP_UPDATE} again.",
                    http_status=409,
                    upstream_message=exc.reason,
                ) from exc
            raise
        keys_removed = sorted(set(existing_keys) - set(given_keys))
        data_keys = given_keys
    else:
        body: dict[str, Any] = {}
        if string_data:
            body["stringData"] = string_data
        if data:
            body["data"] = data
        await core_v1.patch_namespaced_secret(name=name, namespace=namespace, body=body)
        keys_removed = []
        data_keys = sorted(set(existing_keys) | set(given_keys))

    return {
        "name": name,
        "namespace": namespace,
        "type": secret_type,
        "mode": mode,
        "data_keys": data_keys,
        "keys_set": given_keys,
        "keys_removed": keys_removed,
        "updated": True,
    }


async def k8s_secret_delete(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.secret.delete``: delete one Secret.

    A 404 raises :class:`KubernetesSecretNotFoundError`. The result names
    the Secret only.
    """
    name: str = params["name"]
    namespace: str = params["namespace"]
    api_client = await connector._get_api_client(target, operator)
    core_v1 = client.CoreV1Api(api_client)
    try:
        await core_v1.delete_namespaced_secret(name=name, namespace=namespace)
    except ApiException as exc:
        if exc.status == 404:
            raise KubernetesSecretNotFoundError(
                f"Secret {namespace}/{name} does not exist; nothing was deleted."
            ) from exc
        raise
    return {"name": name, "namespace": namespace, "deleted": True}


# ---------------------------------------------------------------------------
# Schemas, guidance and registration rows
# ---------------------------------------------------------------------------

_NAME_PROP: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": "Exact Secret name.",
}
_NAMESPACE_PROP: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": "Namespace the Secret lives in.",
}

_SECRET_UPDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": _NAME_PROP,
        "namespace": _NAMESPACE_PROP,
        "mode": {
            "type": "string",
            "enum": ["patch", "replace"],
            "default": "patch",
            "description": (
                "'patch' (default) sets the given keys and keeps all other keys. "
                "'replace' leaves exactly the given keys; every other key is removed."
            ),
        },
        "string_data": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": "Plaintext key->value map; the API server base64-encodes it.",
        },
        "data": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": "Already-base64-encoded key->value map.",
        },
        "string_data_secret_ref": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": (
                "Map {secret-key: '<scheme>:<path>#<field>'}. MEHO reads each value "
                "from the credential store under your identity and merges it into "
                "string_data (plaintext). The value never passes through you. "
                "'#field' names the payload field (default: the secret key). "
                "Schemeless or 'vault:' is Vault KV-v2; 'gsm:' is GCP Secret Manager."
            ),
        },
        "string_data_secret_mount": {
            "type": "string",
            "description": (
                "Vault KV-v2 mount for string_data_secret_ref reads (default 'secret')."
            ),
        },
        "data_secret_ref": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": (
                "Like string_data_secret_ref, but the stored values are already base64 "
                "and are merged into data."
            ),
        },
        "data_secret_mount": {
            "type": "string",
            "description": "Vault KV-v2 mount for data_secret_ref reads (default 'secret').",
        },
    },
    "required": ["name", "namespace"],
    "anyOf": [
        {"required": ["string_data"]},
        {"required": ["data"]},
        {"required": ["string_data_secret_ref"]},
        {"required": ["data_secret_ref"]},
    ],
    "additionalProperties": False,
    "examples": [
        {
            "name": "db-creds",
            "namespace": "apps",
            "string_data_secret_ref": {"password": "apps/db#password"},
        },
        {
            "name": "registry-pull",
            "namespace": "apps",
            "mode": "replace",
            "data_secret_ref": {".dockerconfigjson": "registry/pull#dockercfg"},
        },
    ],
}

_SECRET_UPDATE_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "namespace": {"type": "string"},
        "type": {"type": "string"},
        "mode": {"type": "string"},
        "data_keys": {"type": "array", "items": {"type": "string"}},
        "keys_set": {"type": "array", "items": {"type": "string"}},
        "keys_removed": {"type": "array", "items": {"type": "string"}},
        "updated": {"type": "boolean"},
    },
    "required": ["name", "namespace", "type", "mode", "data_keys", "keys_set", "updated"],
    "additionalProperties": False,
}

_SECRET_UPDATE_LLM: dict[str, Any] = {
    "when_to_call": (
        "Change the values of a Secret that already exists, for example to rotate a "
        "token or password a workload reads. Prefer the *_secret_ref inputs: MEHO "
        "reads the value from the credential store itself, so it never passes "
        "through you. Use mode 'replace' only to drop keys. For a new Secret use "
        "k8s.secret.create. Requires approval."
    ),
    "preconditions": (
        "The Secret exists and is not immutable (an immutable Secret must be deleted "
        "and created again). At least one value input is given. For a *_secret_ref, "
        "the value is stored in the credential store under the named path and field."
    ),
    "parameter_hints": {
        "name": "Exact Secret name.",
        "namespace": "Required.",
        "mode": "'patch' keeps other keys (default); 'replace' keeps only the given keys.",
        "string_data_secret_ref": "{key: '<scheme>:<path>#<field>'}; read server-side.",
        "data_secret_ref": "Same, for base64 values merged into data.",
    },
    "output_shape": (
        "{name, namespace, type, mode, data_keys, keys_set, keys_removed, updated}. "
        "Key names only, never a value."
    ),
    "next_step": (
        "Call k8s.rollout.restart on the Deployment that reads the Secret if it "
        "loads the value at start (env or envFrom); a mounted volume updates on "
        "its own. If the error says the Secret does not exist, call "
        "k8s.secret.create."
    ),
}

_SECRET_DELETE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"name": _NAME_PROP, "namespace": _NAMESPACE_PROP},
    "required": ["name", "namespace"],
    "additionalProperties": False,
    "examples": [{"name": "old-db-creds", "namespace": "apps"}],
}

_SECRET_DELETE_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "namespace": {"type": "string"},
        "deleted": {"type": "boolean"},
    },
    "required": ["name", "namespace", "deleted"],
    "additionalProperties": False,
}

_SECRET_DELETE_LLM: dict[str, Any] = {
    "when_to_call": (
        "Remove a Secret that nothing needs any more. Kubernetes keeps no copy of "
        "the values. Destructive: call preview_operation first, then call_operation "
        "with the preview_hash it returned; a person's call waits for a second "
        "person, an agent's call is refused."
    ),
    "preconditions": (
        "The Secret exists (a missing Secret fails the preview, so nothing waits "
        "for approval). The preview lists the Pods, Deployments, StatefulSets, "
        "DaemonSets and CronJobs in the namespace that use the Secret; check that "
        "list is empty or that every user can lose the Secret."
    ),
    "parameter_hints": {"name": "Exact Secret name.", "namespace": "Required."},
    "output_shape": "{name, namespace, deleted}.",
    "next_step": (
        "If the preview lists workloads that use the Secret, change them first "
        "(k8s.apply) or keep the Secret. After the delete, k8s.secret.list in the "
        "namespace no longer shows it."
    ),
}

SECRET_WRITE_OPS: tuple[KubernetesOp, ...] = (
    KubernetesOp(
        op_id=_OP_UPDATE,
        handler_attr="k8s_secret_update",
        summary="Change the values of an existing Secret; values are never shown.",
        description=(
            "Sets the given keys of an existing Secret (mode 'patch' keeps the other "
            "keys, 'replace' keeps only the given ones). Values come inline or from "
            "the credential store by reference (*_secret_ref), and are never echoed "
            "back. Never creates; keeps type, labels and annotations. Requires approval."
        ),
        parameter_schema=_SECRET_UPDATE_SCHEMA,
        response_schema=_SECRET_UPDATE_RESPONSE,
        group_key="config",
        tags=("write", "secret", "credential", "dangerous"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=_SECRET_UPDATE_LLM,
    ),
    KubernetesOp(
        op_id=_OP_DELETE,
        handler_attr="k8s_secret_delete",
        summary="Delete one Secret after a preview that lists the workloads using it.",
        description=(
            "Deletes a Secret. Destructive: preview first, then call with the "
            "preview_hash; a second person must approve, and an agent cannot run it. "
            "The preview names the Secret's keys and every Pod, Deployment, "
            "StatefulSet, DaemonSet and CronJob in the namespace that uses it."
        ),
        parameter_schema=_SECRET_DELETE_SCHEMA,
        response_schema=_SECRET_DELETE_RESPONSE,
        group_key="config",
        tags=("write", "secret", "credential", "delete", "destructive"),
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=_SECRET_DELETE_LLM,
    ),
)
