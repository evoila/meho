# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Governed, audit-classified guest-cluster kubeconfig read (#3496).

``k8s.secret.read_to_ref`` reads one named Secret's ``data`` key on a
k8s / vSphere-Supervisor target and stages the decoded value into a Vault
``secret_ref`` under the caller's tenant scope, returning **only** the
``secret_ref`` + provenance metadata. The value never enters the op
result, so it never lands in an agent transcript.

Why this op exists
==================

The shipped WCP SSO auth mode (#2905) establishes only the top-level
Supervisor context; it deliberately does not open guest-cluster
sub-sessions (``wcp.py``). A VKS guest cluster is therefore registered
from its Cluster-API-generated ``<cluster>-kubeconfig`` Secret in the
Supervisor namespace (the admin client-cert kubeconfig, stored under the
``value`` data key). But the k8s connector had ``k8s.secret.create`` and
no **data-returning** Secret read — Secret ``data`` is clamped on
broadcast — so extracting that kubeconfig meant an out-of-band
``kubectl get secret … -o jsonpath``, escaping audit / policy / approval.
This op closes that gap **through the backplane**.

The no-transit design (contrast with the issue's ``read_data`` sketch)
==================================================================

The issue (#3496) sketched a ``read_data`` op that returns the decoded
kubeconfig to the approval-gated caller (redacted only in
audit/broadcast transcripts). This op is the stronger ``read_to_ref``
shape: it borrows the secret broker's core invariant
(``connectors/secret``) — the value is read inside the backplane and
written straight to a Vault ``secret_ref``; the caller receives only the
ref. So the kubeconfig never reaches the ``call_operation`` result / the
model-API transcript at all, not merely the audit row. The round-trip
the issue calls for (stage → register a ``product=k8s`` target →
``k8s.node.list``) is served directly: this op *is* the staging step,
and it writes under the field name the k8s kubeconfig loader
(:func:`~meho_backplane.connectors.kubernetes.kubeconfig.load_kubeconfig_from_vault`)
reads (``kubeconfig`` by default), so a freshly-created ``product=k8s``
target whose ``secret_ref`` is the returned path authenticates out of
the box.

Governance posture (mirrors ``sddc.credential.list``, #2306)
============================================================

* ``safety_level="caution"`` + ``requires_approval=True`` — the policy
  gate parks an ordinary dispatch for operator approval; the read never
  runs unapproved.
* ``op_id`` is pinned in
  :data:`meho_backplane.broadcast.events._CREDENTIAL_READ_OPS`, so
  ``classify_op`` returns ``credential_read`` and the audit + broadcast
  rows collapse to aggregate-only.
* the result is value-free **by construction** (only the ref + a
  SHA-256 + byte length, like the secret broker's ``SecretMaterial``
  provenance), so even the connector-boundary ``credential_read``
  response scrub (#2467) and the defensive Tier-1 engine have no secret
  to strip.

Tenant scope (no duplicated logic)
==================================

The destination path is derived with
:func:`~meho_backplane.connectors.vault.tenant_paths.tenant_secret_ref`
— the same helper ``POST /api/v1/targets`` uses to default an omitted
``secret_ref`` (#1723) — so the returned ref is exactly what a later
``targets create --name <register_as>`` (secret_ref omitted) reads, and
is inside the operator's tenant subtree by construction. The write goes
through the ``vault.kv.put`` handler
(:func:`~meho_backplane.connectors.vault.ops.vault_kv_put`), which runs
:func:`~meho_backplane.connectors.vault.tenant_scope.enforce_tenant_scope`
before any Vault round-trip — so the same default-on guard the
``api/v1/targets.py`` write-time ``secret_ref`` gate mirrors is enforced
here as defense-in-depth, without re-implementing the check (that gate
itself raises ``HTTPException`` and is FastAPI-coupled, so the
connector-appropriate reuse is the shared guard it wraps).

Supervisor case: no server rewrite (corpus-verified)
====================================================

A VKS guest kubeconfig's ``clusters[].cluster.server`` already points at
the guest cluster's own LoadBalancer API-server VIP and is used as-is —
so **no** server-address rewrite is needed here (corpus:
``vsphere-supervisor-services-and-standalone-components.pdf``,
``VCFB1443LV-kubernetes-101-and-the-iaas-control-plane-on-vmware-cloud-fo.pdf``).
The op stages the kubeconfig verbatim.

References
----------
* Task: #3496. Related: #2905 (WCP SSO auth), meho-automation#181
  (the blueprint that stages the kubeconfig + registers the guest target).
* Secret-broker no-transit invariant: ``connectors/secret`` (#1577).
* ``credential_read`` classification: ``broadcast/events.py`` (#2306, #2467).
* k8s Secret API:
  https://kubernetes.io/docs/reference/kubernetes-api/config-and-storage-resources/secret-v1/
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from typing import TYPE_CHECKING, Any

from kubernetes_asyncio import client

from meho_backplane.connectors.kubernetes.ops import KubernetesOp

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.kubernetes.connector import KubernetesConnector
    from meho_backplane.connectors.kubernetes.kubeconfig import KubernetesTargetLike

__all__ = [
    "DEFAULT_KUBECONFIG_DATA_KEY",
    "DEFAULT_STAGE_FIELD",
    "SECRET_READ_OPS",
    "KubernetesSecretDataError",
    "k8s_secret_read_to_ref",
]

#: The Secret ``data`` key a Cluster-API-generated ``<cluster>-kubeconfig``
#: Secret stores the admin kubeconfig under (corpus-verified for VKS /
#: vSphere Supervisor; the upstream Cluster API ``KubeconfigDataName``
#: constant is likewise ``"value"``).
DEFAULT_KUBECONFIG_DATA_KEY = "value"

#: The field name the staged value is written under in the destination
#: Vault secret. The k8s kubeconfig loader
#: (:func:`~meho_backplane.connectors.kubernetes.kubeconfig.load_kubeconfig_from_vault`)
#: reads the ``kubeconfig`` field, so a ``product=k8s`` target created
#: against the returned ``secret_ref`` authenticates without any extra
#: staging step.
DEFAULT_STAGE_FIELD = "kubeconfig"


class KubernetesSecretDataError(ValueError):
    """The requested Secret ``data`` key is absent or not decodable.

    Raised when the named Secret carries no ``data`` map, the requested
    key is missing, or its value is not valid base64 / UTF-8. The message
    names the Secret + key (never the value). Subclasses :class:`ValueError`
    so the dispatcher's ``connector_error`` envelope tags
    ``extras.exception_class="KubernetesSecretDataError"``, matching the
    sibling ``k8s.secret.create`` error shapes.
    """


def _decode_secret_value(
    data: dict[str, str] | None, *, name: str, namespace: str, key: str
) -> str:
    """Return the UTF-8-decoded value of ``data[key]`` from a Secret.

    ``V1Secret.data`` values are base64-encoded strings. A missing map,
    a missing key, or a value that is not valid base64 / UTF-8 raises
    :class:`KubernetesSecretDataError` naming the Secret + key but never
    the value.
    """
    if not data or key not in data:
        available = sorted(data) if data else []
        raise KubernetesSecretDataError(
            f"Secret {namespace}/{name} has no data key {key!r} "
            f"(present keys: {available}). Pass data_key to name the key that "
            f"holds the kubeconfig (a Cluster-API guest kubeconfig uses 'value')."
        )
    try:
        return base64.b64decode(data[key], validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise KubernetesSecretDataError(
            f"Secret {namespace}/{name} data key {key!r} is not valid "
            f"base64-encoded UTF-8 ({type(exc).__name__})"
        ) from exc


async def k8s_secret_read_to_ref(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.secret.read_to_ref`` — stage a Secret value to a Vault ref.

    Reads ``data[data_key]`` from the named Secret on *target* (a
    Supervisor / k8s cluster), decodes it, and writes it to a Vault
    ``secret_ref`` derived for the caller's tenant via
    :func:`~meho_backplane.connectors.vault.tenant_paths.tenant_secret_ref`.
    The write rides the ``vault.kv.put`` handler so the tenant-scope guard
    runs under the operator's own Vault identity. Returns only the
    ``secret_ref`` + provenance (SHA-256, byte length) — never the value.
    """
    name: str = params["name"]
    namespace: str = params["namespace"]
    data_key: str = params.get("data_key") or DEFAULT_KUBECONFIG_DATA_KEY
    register_as: str = params["register_as"]
    field: str = params.get("field") or DEFAULT_STAGE_FIELD

    # Lazy imports: keep this metadata/handler module free of import-time
    # coupling to the vault connector + settings (it is imported while the
    # kubernetes op tuple is built at module load); the handler only runs
    # at dispatch, long after every connector module is imported.
    from meho_backplane.connectors._shared.vault_creds import DEFAULT_KV_MOUNT
    from meho_backplane.connectors.vault.ops import vault_kv_put
    from meho_backplane.connectors.vault.tenant_paths import tenant_secret_ref

    api_client = await connector._get_api_client(target, operator)
    core_v1 = client.CoreV1Api(api_client)
    secret = await core_v1.read_namespaced_secret(name=name, namespace=namespace)
    value = _decode_secret_value(secret.data, name=name, namespace=namespace, key=data_key)

    dest_ref = tenant_secret_ref(operator.tenant_id, register_as)
    # vault_kv_put runs enforce_tenant_scope (#1643) under the operator's
    # identity before the Vault round-trip; dest_ref is inside the tenant
    # subtree by construction, so this is defense-in-depth, not the gate.
    await vault_kv_put(
        operator,
        None,
        {"mount": DEFAULT_KV_MOUNT, "path": dest_ref, "data": {field: value}},
    )

    value_bytes = value.encode("utf-8")
    return {
        "secret_ref": dest_ref,
        "field": field,
        "registered_as": register_as,
        "name": name,
        "namespace": namespace,
        "data_key": data_key,
        "value_sha256": hashlib.sha256(value_bytes).hexdigest(),
        "length": len(value_bytes),
    }


_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Exact Secret name on the source target — e.g. the "
                "'<cluster>-kubeconfig' Secret a VKS guest cluster publishes."
            ),
        },
        "namespace": {
            "type": "string",
            "minLength": 1,
            "description": "Namespace the Secret lives in (the Supervisor namespace for VKS).",
        },
        "data_key": {
            "type": "string",
            "minLength": 1,
            "default": DEFAULT_KUBECONFIG_DATA_KEY,
            "description": (
                "The Secret 'data' key holding the kubeconfig. Defaults to 'value' "
                "(the Cluster-API guest-kubeconfig convention)."
            ),
        },
        "register_as": {
            "type": "string",
            "minLength": 1,
            "maxLength": 253,
            "description": (
                "The target name the guest cluster will be registered under. The "
                "destination Vault path is derived as tenants/<tenant_id>/<register_as> "
                "— exactly what 'targets create --name <register_as>' (secret_ref "
                "omitted) reads, so the staged kubeconfig round-trips into that target."
            ),
        },
        "field": {
            "type": "string",
            "minLength": 1,
            "default": DEFAULT_STAGE_FIELD,
            "description": (
                "The field name the value is written under in the destination Vault "
                "secret. Defaults to 'kubeconfig' — the field the k8s kubeconfig "
                "loader reads, so a product=k8s target on the returned secret_ref "
                "authenticates as-is."
            ),
        },
    },
    "required": ["name", "namespace", "register_as"],
    "additionalProperties": False,
}


_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "secret_ref": {"type": "string"},
        "field": {"type": "string"},
        "registered_as": {"type": "string"},
        "name": {"type": "string"},
        "namespace": {"type": "string"},
        "data_key": {"type": "string"},
        "value_sha256": {"type": "string"},
        "length": {"type": "integer"},
    },
    "required": [
        "secret_ref",
        "field",
        "registered_as",
        "name",
        "namespace",
        "data_key",
        "value_sha256",
        "length",
    ],
    "additionalProperties": False,
}


_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Register a VKS / vSphere-Supervisor guest cluster as a second k8s "
        "target WITHOUT ever putting its kubeconfig in your context. Reads the "
        "'<cluster>-kubeconfig' Secret on the Supervisor target and stages the "
        "decoded kubeconfig to a Vault secret_ref under your tenant; the value "
        "is NEVER returned. Then create a product=k8s target named <register_as> "
        "(secret_ref omitted — it defaults to the returned path). Approval-gated; "
        "audit + broadcast collapse to aggregate-only (credential_read)."
    ),
    "parameter_hints": {
        "name": "The source Secret name (e.g. 'my-cluster-kubeconfig').",
        "namespace": "The Supervisor namespace the guest cluster lives in.",
        "data_key": "Which data key holds the kubeconfig; defaults to 'value'.",
        "register_as": "The guest target name; the Vault path is derived from it.",
        "field": "Destination Vault field; defaults to 'kubeconfig' (loader-ready).",
    },
    "output_shape": (
        "{secret_ref, field, registered_as, name, namespace, data_key, "
        "value_sha256, length} — the kubeconfig itself is NEVER in the result; "
        "only its SHA-256 + byte length as provenance."
    ),
}


SECRET_READ_OPS: tuple[KubernetesOp, ...] = (
    KubernetesOp(
        op_id="k8s.secret.read_to_ref",
        handler_attr="k8s_secret_read_to_ref",
        summary="Stage a Secret value to a tenant-scoped Vault secret_ref; return only the ref.",
        description=(
            "Reads ``data[data_key]`` (default 'value') from the named Secret "
            "on a k8s / vSphere-Supervisor target, decodes it, and writes it to "
            "a Vault ``secret_ref`` derived for the caller's tenant "
            "(tenants/<tenant_id>/<register_as>) via the ``vault.kv.put`` "
            "handler (which enforces the tenant-scope guard). Returns ONLY the "
            "secret_ref + provenance (SHA-256, byte length) — the value never "
            "enters the result, so a guest-cluster kubeconfig can be registered "
            "as a second k8s target without the kubeconfig ever reaching an "
            "agent transcript. Purpose-built for extracting a VKS guest "
            "cluster's '<cluster>-kubeconfig' Secret from the Supervisor "
            "namespace. Classified credential_read (audit + broadcast collapse "
            "to aggregate-only) and requires approval."
        ),
        parameter_schema=_PARAMETER_SCHEMA,
        response_schema=_RESPONSE_SCHEMA,
        group_key="config",
        tags=("read", "secret", "credential", "kubeconfig", "caution"),
        safety_level="caution",
        requires_approval=True,
        llm_instructions=_LLM_INSTRUCTIONS,
    ),
)
