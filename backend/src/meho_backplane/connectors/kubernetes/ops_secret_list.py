# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""List and describe Kubernetes Secrets by name only: ``k8s.secret.list`` / ``k8s.secret.info``.

MEHO could create, change, delete and copy a Secret, but it could not
list Secrets or look at one. So before a change an operator had to run
``kubectl`` by hand to see which keys a Secret has. These two safe reads
close that gap.

Never a value
=============

The answer is built from names only:

* the Secret's ``data`` and ``stringData`` give their **key names** and,
  in ``k8s.secret.info``, the size of each value in bytes;
* labels keep their names and values;
* annotations give their **names only**. An annotation value can hold a
  value of the Secret: ``kubectl apply`` writes the whole last manifest,
  values included, into
  ``kubectl.kubernetes.io/last-applied-configuration``;
* owner references give their ``kind`` and ``name``.

:func:`_names_only` reads these fields from the ``V1Secret`` and copies
nothing else, so no value is ever put into the answer. The answer then
passes through
:func:`~meho_backplane.connectors.kubernetes.redaction.redact_kubernetes_payload`
as a second net: if a later change ever added a ``data`` or
``stringData`` map, its values would still be replaced.

Kubernetes rights
=================

The service account of the target needs ``list`` on ``secrets`` (in the
namespace, or cluster-wide for ``all_namespaces``) for the list, and
``get`` on ``secrets`` for the info. The Kubernetes API sends the values
to MEHO on both calls; MEHO drops them before the answer is built.

References
----------
* Secret API:
  https://kubernetes.io/docs/reference/kubernetes-api/config-and-storage-resources/secret-v1/
* ``kubernetes_asyncio`` 36.1.0 ``CoreV1Api``:
  ``list_namespaced_secret(namespace, label_selector=...)``,
  ``list_secret_for_all_namespaces(label_selector=...)``,
  ``read_namespaced_secret(name, namespace)``. ``V1Secret`` carries
  ``data`` (base64 text per key), ``string_data``, ``type``,
  ``immutable`` and ``metadata``.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from typing import TYPE_CHECKING, Any

from kubernetes_asyncio import client

from meho_backplane.connectors.kubernetes.ops import KubernetesOp
from meho_backplane.connectors.kubernetes.ops_listparams import (
    ALL_NAMESPACES_PARAM,
    LABEL_SELECTOR_PARAM,
    NAMESPACE_PARAM,
    NAMESPACE_XOR_ALL_NAMESPACES,
)
from meho_backplane.connectors.kubernetes.redaction import SECRET_KIND, redact_kubernetes_payload

if TYPE_CHECKING:
    from kubernetes_asyncio.client.models import V1Secret

    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.kubernetes.connector import KubernetesConnector
    from meho_backplane.connectors.kubernetes.kubeconfig import KubernetesTargetLike

__all__ = [
    "SECRET_LIST_OPS",
    "k8s_secret_info",
    "k8s_secret_list",
    "secret_info",
    "secret_list_row",
]


def _value_size(value: Any, *, base64_encoded: bool) -> int | None:
    """Return the size of one Secret value in bytes, never the value.

    A ``data`` value is base64 text, so its size is the length of the
    decoded bytes. A ``stringData`` value is plain text. ``None`` when a
    ``data`` value is not valid base64.
    """
    if not isinstance(value, str):
        return None
    if not base64_encoded:
        return len(value.encode("utf-8"))
    try:
        return len(base64.b64decode(value, validate=True))
    except (binascii.Error, ValueError):
        return None


def _key_sizes(secret: V1Secret) -> dict[str, int | None]:
    """Map each key name of *secret* to the size of its value in bytes."""
    sizes: dict[str, int | None] = {}
    for key, value in (secret.string_data or {}).items():
        sizes[key] = _value_size(value, base64_encoded=False)
    # ``data`` wins over ``stringData`` for the same key: it is what is stored.
    for key, value in (secret.data or {}).items():
        sizes[key] = _value_size(value, base64_encoded=True)
    return dict(sorted(sizes.items()))


def _created(timestamp: datetime | None) -> str | None:
    return timestamp.isoformat() if timestamp is not None else None


def _names_only(secret: V1Secret) -> dict[str, Any]:
    """Read the name-level fields of *secret*. No value is copied.

    Key names come from ``data`` and ``stringData``; annotation names
    come from the annotation map; the values of both are never read
    into the result.
    """
    metadata = secret.metadata
    keys = sorted(set(secret.data or {}) | set(secret.string_data or {}))
    owners = list(metadata.owner_references or []) if metadata is not None else []
    return {
        "name": metadata.name if metadata is not None else None,
        "namespace": metadata.namespace if metadata is not None else None,
        "type": secret.type,
        "keys": keys,
        "key_count": len(keys),
        "labels": dict(metadata.labels or {}) if metadata is not None else {},
        "annotation_keys": sorted(metadata.annotations or {}) if metadata is not None else [],
        "owner_count": len(owners),
        "created": _created(metadata.creation_timestamp if metadata is not None else None),
        "resource_version": metadata.resource_version if metadata is not None else None,
        "_owners": [{"kind": ref.kind, "name": ref.name} for ref in owners],
    }


def _second_net(view: dict[str, Any]) -> dict[str, Any]:
    """Run the structural Secret redactor over *view* as a second net.

    The view is marked ``kind: Secret`` for the walk, so a ``data`` or
    ``stringData`` map that ever reached it would have its values
    replaced. The marker is removed again before the view is returned.
    """
    redacted: dict[str, Any] = redact_kubernetes_payload({**view, "kind": SECRET_KIND})
    redacted.pop("kind")
    return redacted


def secret_list_row(secret: V1Secret) -> dict[str, Any]:
    """Project a ``V1Secret`` into one ``k8s.secret.list`` row: names only."""
    view = _names_only(secret)
    view.pop("_owners")
    return _second_net(view)


def secret_info(secret: V1Secret) -> dict[str, Any]:
    """Project a ``V1Secret`` into the ``k8s.secret.info`` answer: names and sizes only."""
    view = _names_only(secret)
    view["owners"] = view.pop("_owners")
    view["immutable"] = bool(secret.immutable)
    view["key_sizes"] = _key_sizes(secret)
    return _second_net(view)


async def k8s_secret_list(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.secret.list``: one names-only row per Secret."""
    kwargs: dict[str, Any] = {}
    if params.get("label_selector"):
        kwargs["label_selector"] = params["label_selector"]
    api_client = await connector._get_api_client(target, operator)
    core_v1 = client.CoreV1Api(api_client)
    if params.get("all_namespaces"):
        resp = await core_v1.list_secret_for_all_namespaces(**kwargs)
    else:
        resp = await core_v1.list_namespaced_secret(namespace=params["namespace"], **kwargs)
    rows = [secret_list_row(secret) for secret in resp.items]
    return {"rows": rows, "total": len(rows)}


async def k8s_secret_info(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.secret.info``: one Secret, names and sizes only."""
    api_client = await connector._get_api_client(target, operator)
    core_v1 = client.CoreV1Api(api_client)
    secret = await core_v1.read_namespaced_secret(
        name=params["name"], namespace=params["namespace"]
    )
    return secret_info(secret)


# ---------------------------------------------------------------------------
# Op metadata
# ---------------------------------------------------------------------------

_ROW_PROPERTIES: dict[str, Any] = {
    "name": {"type": ["string", "null"]},
    "namespace": {"type": ["string", "null"]},
    "type": {"type": ["string", "null"]},
    "keys": {"type": "array", "items": {"type": "string"}},
    "key_count": {"type": "integer"},
    "labels": {"type": "object", "additionalProperties": {"type": "string"}},
    "annotation_keys": {"type": "array", "items": {"type": "string"}},
    "owner_count": {"type": "integer"},
    "created": {"type": ["string", "null"]},
    "resource_version": {"type": ["string", "null"]},
}

_LIST_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "namespace": NAMESPACE_PARAM,
        "all_namespaces": ALL_NAMESPACES_PARAM,
        "label_selector": LABEL_SELECTOR_PARAM,
    },
    "oneOf": NAMESPACE_XOR_ALL_NAMESPACES,
    "additionalProperties": False,
    "examples": [
        {"namespace": "apps"},
        {"all_namespaces": True, "label_selector": "app=web"},
    ],
}

_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": _ROW_PROPERTIES,
                "required": list(_ROW_PROPERTIES),
                "additionalProperties": False,
            },
        },
        "total": {"type": "integer"},
    },
    "required": ["rows", "total"],
    "additionalProperties": False,
}

_INFO_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "pattern": r"\S",
            "description": "Exact Secret name.",
        },
        "namespace": {
            "type": "string",
            "minLength": 1,
            "pattern": r"\S",
            "description": "Namespace the Secret lives in.",
        },
    },
    "required": ["name", "namespace"],
    "additionalProperties": False,
    "examples": [{"name": "db-creds", "namespace": "apps"}],
}

_INFO_PROPERTIES: dict[str, Any] = {
    **_ROW_PROPERTIES,
    "immutable": {"type": "boolean"},
    "owners": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "kind": {"type": ["string", "null"]},
                "name": {"type": ["string", "null"]},
            },
            "required": ["kind", "name"],
            "additionalProperties": False,
        },
    },
    "key_sizes": {
        "type": "object",
        "additionalProperties": {"type": ["integer", "null"]},
    },
}

_INFO_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": _INFO_PROPERTIES,
    "required": list(_INFO_PROPERTIES),
    "additionalProperties": False,
}

_NO_VALUE = (
    "No value is ever shown: not the Secret's values and not the annotation "
    "values (an annotation can hold a copy of the values)."
)

_RIGHTS_LIST = "The target's service account needs 'list' on 'secrets'."
_RIGHTS_INFO = "The target's service account needs 'get' on 'secrets'."

_LIST_LLM: dict[str, Any] = {
    "when_to_call": (
        "List the Secrets in a namespace (or in all namespaces) and see each "
        "Secret's type and key names, for example before k8s.secret.update, to "
        "learn which keys a Secret has. " + _NO_VALUE
    ),
    "parameter_hints": {
        "namespace": "Required unless all_namespaces is true.",
        "all_namespaces": "Pass true to list in every namespace; do not pass namespace then.",
        "label_selector": "Optional label filter, for example 'app=web'.",
    },
    "output_shape": (
        "{rows: [{name, namespace, type, keys, key_count, labels, annotation_keys, "
        "owner_count, created, resource_version}], total}. A long list comes back "
        "as a link to the stored result; read it with result_query."
    ),
    "next_step": (
        "Call k8s.secret.info for one Secret to see its owners, whether it is "
        "immutable and the size of each value. To copy a value into the "
        "credential store without showing it, call k8s.secret.read_to_ref. " + _RIGHTS_LIST
    ),
}

_INFO_LLM: dict[str, Any] = {
    "when_to_call": (
        "Look at one Secret before changing it: its type, key names, labels, "
        "annotation names, owners, whether it is immutable and the size of each "
        "value in bytes. " + _NO_VALUE
    ),
    "parameter_hints": {
        "name": "Exact Secret name (from k8s.secret.list).",
        "namespace": "Required.",
    },
    "output_shape": (
        "{name, namespace, type, keys, key_count, labels, annotation_keys, "
        "owner_count, created, resource_version, immutable, owners: [{kind, name}], "
        "key_sizes: {<key>: <bytes>}}."
    ),
    "next_step": (
        "To change values, call k8s.secret.update (mode 'patch' keeps the other "
        "keys, labels and annotations). An immutable Secret cannot be changed; "
        "delete it and create it again. " + _RIGHTS_INFO
    ),
}

SECRET_LIST_OPS: tuple[KubernetesOp, ...] = (
    KubernetesOp(
        op_id="k8s.secret.list",
        handler_attr="k8s_secret_list",
        summary="List Secrets with their type and key names; values are never shown.",
        description=(
            "Lists the Secrets in one namespace or in all namespaces. Each row has the "
            "name, type, key names, labels, annotation names and owner count. No value "
            "and no annotation value is returned. Read-only."
        ),
        parameter_schema=_LIST_PARAMETER_SCHEMA,
        response_schema=_LIST_RESPONSE_SCHEMA,
        group_key="config",
        tags=("read-only", "config", "secret"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=_LIST_LLM,
    ),
    KubernetesOp(
        op_id="k8s.secret.info",
        handler_attr="k8s_secret_info",
        summary="Describe one Secret by key names and value sizes; values are never shown.",
        description=(
            "Reads one Secret and returns its type, key names, the size of each value "
            "in bytes, labels, annotation names, owners (kind and name) and whether it "
            "is immutable. No value and no annotation value is returned. Read-only."
        ),
        parameter_schema=_INFO_PARAMETER_SCHEMA,
        response_schema=_INFO_RESPONSE_SCHEMA,
        group_key="config",
        tags=("read-only", "config", "secret"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=_INFO_LLM,
    ),
)
