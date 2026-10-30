# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``k8s.secret.compare_to_ref``: check a Secret key against its Vault copy.

A Helm chart often reads a password from a Kubernetes Secret while the
same password is kept in Vault. After a change the operator must prove
the two copies match. This op answers that without showing the value.

How it works
============

* The Secret is read on the target with the connector's normal Kubernetes
  access (the same rules as every other ``k8s.*`` op).
* The Vault copy is read with the caller's own Vault login and tenant
  guard, the same way ``k8s.secret.read_to_ref`` reaches Vault: no Vault
  target, so the deployment's Vault and the default role are used.
* The two values are compared with :func:`hmac.compare_digest` (constant
  time) and dropped. The Secret value is base64-decoded first, so it is
  compared with the plain text stored in Vault. A Vault value that is not
  a string (a number, ``null``, a list) never matches the Secret.
* The answer holds ``same`` and two copies (the Secret first, then Vault),
  each with ``found``, ``matches_first``, its version (the Secret's
  ``resourceVersion`` or the Vault version) and a ``reason`` when it was
  not read. Never a value, never a hash.

A copy the caller may not read answers ``found: false, reason:
"forbidden"`` (Kubernetes 403, Vault 403, or a Vault path outside the
caller's tenant); the call does not fail. The op id is on
``broadcast.events._CREDENTIAL_READ_OPS``, so the flight recorder never
stores the Secret body and the broadcast feed shows only the outcome.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

from kubernetes_asyncio import client
from kubernetes_asyncio.client.rest import ApiException

from meho_backplane.connectors._shared.vault_creds import DEFAULT_KV_MOUNT
from meho_backplane.connectors.kubernetes.ops import KubernetesOp

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.kubernetes.connector import KubernetesConnector
    from meho_backplane.connectors.kubernetes.kubeconfig import KubernetesTargetLike

__all__ = ["SECRET_COMPARE_OPS", "k8s_secret_compare_to_ref"]

_OP_ID = "k8s.secret.compare_to_ref"

#: Kubernetes HTTP status -> ``reason`` for a Secret that could not be read.
_STATUS_REASONS: dict[int, str] = {403: "forbidden", 404: "not_found"}

#: Every ``reason`` a copy can carry; same values as ``vault.kv.compare``.
_REASONS = ("forbidden", "not_found", "key_not_found")


async def _read_secret_copy(
    core_v1: Any, *, name: str, namespace: str, key: str
) -> tuple[dict[str, Any], bytes | None]:
    """Read one Secret key; return its report and the decoded bytes (or ``None``)."""
    report: dict[str, Any] = {
        "source": "k8s",
        "namespace": namespace,
        "name": name,
        "key": key,
        "found": False,
        "version": None,
        "reason": None,
    }
    try:
        secret = await core_v1.read_namespaced_secret(name=name, namespace=namespace)
    except ApiException as exc:
        reason = _STATUS_REASONS.get(exc.status or 0)
        if reason is None:
            raise
        return {**report, "reason": reason}, None

    metadata = getattr(secret, "metadata", None)
    report["version"] = getattr(metadata, "resource_version", None)
    data = secret.data or {}
    if key not in data:
        return {**report, "reason": "key_not_found"}, None
    return {**report, "found": True}, base64.b64decode(data[key], validate=True)


async def k8s_secret_compare_to_ref(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.secret.compare_to_ref``: Secret key vs one Vault copy."""
    # Lazy imports, as in ``k8s.secret.read_to_ref``: this module is imported
    # while the kubernetes op tuple is built, so it stays free of import-time
    # coupling to the vault connector package.
    from meho_backplane.connectors.vault.kv_compare import (
        compare_copies,
        read_kv_copy,
        text_value,
    )
    from meho_backplane.connectors.vault.target_auth import vault_client_for_target

    api_client = await connector._get_api_client(target, operator)
    secret_report, secret_raw = await _read_secret_copy(
        client.CoreV1Api(api_client),
        name=params["name"],
        namespace=params["namespace"],
        key=params["key"],
    )
    secret_copy = (secret_report, None if secret_raw is None else text_value(secret_raw))
    del secret_raw
    async with vault_client_for_target(operator, None) as vault_client:
        vault_report, vault_value = await read_kv_copy(
            vault_client, operator, params["ref"], op_id=_OP_ID
        )
    answer = compare_copies([secret_copy, ({"source": "vault", **vault_report}, vault_value)])
    del secret_copy, vault_value
    return answer


_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "namespace": {
            "type": "string",
            "minLength": 1,
            "description": "Namespace the Secret lives in.",
        },
        "name": {"type": "string", "minLength": 1, "description": "Exact Secret name."},
        "key": {
            "type": "string",
            "minLength": 1,
            "description": "The Secret data key whose value is compared.",
        },
        "ref": {
            "type": "object",
            "description": "The Vault copy {path, key, mount?} the Secret value should match.",
            "properties": {
                "mount": {
                    "type": "string",
                    "minLength": 1,
                    "pattern": "^(?=.*\\S)[^/]+$",
                    "default": DEFAULT_KV_MOUNT,
                    "description": "KV v2 mount (one path segment). Defaults to 'secret'.",
                },
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "pattern": "\\S",
                    "description": "Secret path below the mount; no leading slash, no mount.",
                },
                "key": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The key inside the Vault secret whose value is compared.",
                },
            },
            "required": ["path", "key"],
            "additionalProperties": False,
        },
    },
    "required": ["namespace", "name", "key", "ref"],
    "additionalProperties": False,
    "examples": [
        {
            "namespace": "keycloak",
            "name": "keycloak-admin",
            "key": "admin-password",
            "ref": {"path": "keycloak/admin", "key": "password"},
        }
    ],
}

_COPY_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source": {"type": "string", "enum": ["k8s", "vault"]},
        "namespace": {"type": "string"},
        "name": {"type": "string"},
        "mount": {"type": "string"},
        "path": {"type": "string"},
        "key": {"type": "string"},
        "found": {"type": "boolean"},
        "matches_first": {"type": "boolean"},
        "version": {
            "type": ["integer", "string", "null"],
            "description": "Secret resourceVersion (k8s) or KV v2 version (vault).",
        },
        "reason": {"type": ["string", "null"], "enum": [*_REASONS, None]},
    },
    "required": ["source", "key", "found", "matches_first", "version", "reason"],
    "additionalProperties": False,
}

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "same": {
            "type": "boolean",
            "description": "True only when both copies were found and the values match.",
        },
        "copies": {"type": "array", "items": _COPY_RESULT_SCHEMA},
    },
    "required": ["same", "copies"],
    "additionalProperties": False,
}

_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Check that a Kubernetes Secret key holds the same value as its copy in Vault, "
        "for example after a password change that a Helm chart reads from the Secret. "
        "The value never reaches you: the answer says only 'same' or 'different'. To "
        "compare Vault paths with each other, use vault.kv.compare."
    ),
    "parameter_hints": {
        "namespace": "Namespace of the Secret.",
        "name": "Exact Secret name.",
        "key": "The Secret data key.",
        "ref": "The Vault copy {path, key, mount?}; path is below the mount.",
    },
    "output_shape": (
        "{same, copies: [k8s copy, vault copy]}. Each copy has source, key, found, "
        "matches_first, version (resourceVersion or Vault version) and reason "
        "('forbidden', 'not_found', 'key_not_found' or null). Never a value or a hash."
    ),
    "next_step": (
        "If same is false, fix the stale copy: k8s.secret.update for the Secret, "
        "vault.kv.put or vault.kv.patch for Vault. Then restart the workload that "
        "reads the Secret at start (k8s.rollout.restart) and compare again."
    ),
    # Keep the verdict inline if the copies list is ever stored behind a
    # handle; the reducer copies no scalar of a credential-class op by itself.
    "result_scalars": {"keys": ["same"]},
}


SECRET_COMPARE_OPS: tuple[KubernetesOp, ...] = (
    KubernetesOp(
        op_id=_OP_ID,
        handler_attr="k8s_secret_compare_to_ref",
        summary="Check that a Secret key holds the same value as its Vault copy.",
        description=(
            "Reads the Secret key on the target and the Vault copy with the caller's own "
            "Vault login, and compares the values in constant time. Returns only "
            "same/different and versions, never a value or a hash. A copy the caller "
            "may not read answers found=false."
        ),
        parameter_schema=_PARAMETER_SCHEMA,
        response_schema=_RESPONSE_SCHEMA,
        group_key="config",
        tags=("read-only", "secret", "credential-read", "compare"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=_LLM_INSTRUCTIONS,
    ),
)
