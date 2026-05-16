# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Config ops -- ``k8s.configmap.list/info``.

G3.2-T4 (#324) of Initiative #320. Two read-only "what's configured?"
ops layered on top of the T1 / T2 / T5 base substrate:

* ``k8s.configmap.list [--namespace X]`` -- ``CoreV1Api.list_namespaced_config_map``.
  Per-row shape carries ``keys`` (the configmap's data keys) **but
  never the values**. The split is privacy-relevant: configmaps in the
  wild often blur into sensitive territory (registry credentials in
  ``imagePullSecrets``-adjacent maps, OIDC client secrets in
  ``argocd-cm``, vendor licence keys), and the operator's list view
  should not bulk-broadcast the bytes through the SSE feed during a
  routine "what's configured here?" scan. Operators wanting values
  call ``.info`` per configmap so the audit row records the targeted
  read instead of the bulk dump.

* ``k8s.configmap.info <name> --namespace X`` -- ``CoreV1Api.read_namespaced_config_map``.
  Returns the full configmap, including ``data`` and ``binary_data``.
  Carries ``op_class=read`` for v0.2; G6.3 may upgrade specific
  configmap patterns (managed-by ``secret-translator``, names matching
  ``*-secret-config``) to a ``sensitive-read`` audit classifier.

Row-shape helpers (:func:`configmap_list_row`, :func:`configmap_info`)
are pure functions over :mod:`kubernetes_asyncio.client.models`
instances so the unit tests pin the wire shape against synthetic
fixtures without booting an event loop.

The sibling :mod:`ops_events` module hosts the third T4 op
(``k8s.event.list``); it landed in its own module so this file fits
under the 600-line code-quality cap.

References
----------
* Parent task: G3.2-T4 (#324).
* Parent Initiative: G3.2 (#320), kubernetes-asyncio typed connector.
* k8s ConfigMap API: https://kubernetes.io/docs/reference/kubernetes-api/config-and-storage-resources/config-map-v1/
* ``kubernetes_asyncio.CoreV1Api``:
  https://github.com/tomplus/kubernetes_asyncio/blob/master/kubernetes_asyncio/docs/CoreV1Api.md
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.kubernetes.ops import KubernetesOp
from meho_backplane.connectors.kubernetes.ops_core import age_seconds

if TYPE_CHECKING:
    from kubernetes_asyncio.client.models import V1ConfigMap

__all__ = [
    "CONFIG_OPS",
    "K8S_CONFIGMAP_INFO_LLM_INSTRUCTIONS",
    "K8S_CONFIGMAP_INFO_PARAMETER_SCHEMA",
    "K8S_CONFIGMAP_INFO_RESPONSE_SCHEMA",
    "K8S_CONFIGMAP_LIST_LLM_INSTRUCTIONS",
    "K8S_CONFIGMAP_LIST_PARAMETER_SCHEMA",
    "K8S_CONFIGMAP_LIST_RESPONSE_SCHEMA",
    "configmap_info",
    "configmap_list_row",
]


# ---------------------------------------------------------------------------
# ConfigMap helpers
# ---------------------------------------------------------------------------


def configmap_list_row(
    cm: V1ConfigMap,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Project a :class:`V1ConfigMap` into the list-op row shape -- **keys only, no values**.

    Critical contract: this helper deliberately omits ``data`` /
    ``binary_data`` values. The list op is the privacy-safe entry
    point; operators that need values call ``k8s.configmap.info``
    per configmap so the audit row records the targeted read instead
    of the bulk dump.

    The ``keys`` list is the **sorted union** of ``data`` keys and
    ``binary_data`` keys -- both surfaces appear as configmap keys to
    operators; the binary/text split is an implementation detail of
    how the configmap was created (``kubectl create configmap --from-file``
    chooses one or the other based on the file's content type).

    ``age_seconds`` follows the same UTC-aware mapping the namespace /
    node rows use; see
    :func:`~meho_backplane.connectors.kubernetes.ops_core.age_seconds`.
    """
    metadata = cm.metadata
    keys: set[str] = set()
    if cm.data:
        keys.update(cm.data.keys())
    if cm.binary_data:
        keys.update(cm.binary_data.keys())
    return {
        "name": metadata.name if metadata is not None else None,
        "namespace": metadata.namespace if metadata is not None else None,
        "keys": sorted(keys),
        "age_seconds": age_seconds(
            metadata.creation_timestamp if metadata is not None else None,
            now=now,
        ),
    }


def configmap_info(
    cm: V1ConfigMap,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Project a :class:`V1ConfigMap` into the info-op shape -- full data + binary_data.

    Counterpart to :func:`configmap_list_row`. This is the targeted
    read shape: the operator named a specific configmap and the audit
    row records ``op_id='k8s.configmap.info'`` + the configmap name in
    ``params_hash``, so a future post-incident audit can identify
    "who read which configmap when?". The bulk list never exposes
    values, only this op does.

    ``data`` and ``binary_data`` are forwarded as plain dicts (cast
    from the API's ``Optional[Dict[str, str]]``) so an empty configmap
    surfaces as ``{}`` / ``{}`` rather than ``None``. ``binary_data``
    values are the API's base64-encoded strings -- ``kubernetes_asyncio``
    does not auto-decode binary payloads, and the operator-facing
    wire shape preserves the encoding so a downstream caller can
    decide whether to decode (and how to render the result).
    """
    metadata = cm.metadata
    return {
        "name": metadata.name if metadata is not None else None,
        "namespace": metadata.namespace if metadata is not None else None,
        "data": dict(cm.data or {}),
        "binary_data": dict(cm.binary_data or {}),
        "metadata": {
            "labels": (dict(metadata.labels or {}) if metadata is not None else {}),
            "annotations": (dict(metadata.annotations or {}) if metadata is not None else {}),
            "age_seconds": age_seconds(
                metadata.creation_timestamp if metadata is not None else None,
                now=now,
            ),
        },
    }


# ---------------------------------------------------------------------------
# Op metadata -- schemas + llm_instructions + KubernetesOp rows.
# ---------------------------------------------------------------------------


_NAMESPACE_PARAM_SCHEMA: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": r"\S",
    "description": "Namespace to list within.",
}


K8S_CONFIGMAP_LIST_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "namespace": _NAMESPACE_PARAM_SCHEMA,
    },
    "required": ["namespace"],
    "additionalProperties": False,
}


K8S_CONFIGMAP_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": ["string", "null"]},
                    "namespace": {"type": ["string", "null"]},
                    "keys": {"type": "array", "items": {"type": "string"}},
                    "age_seconds": {"type": ["integer", "null"]},
                },
                "required": ["name", "namespace", "keys", "age_seconds"],
                "additionalProperties": False,
            },
        },
        "total": {"type": "integer"},
    },
    "required": ["rows", "total"],
    "additionalProperties": False,
}


K8S_CONFIGMAP_LIST_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Call when the operator asks 'what configmaps are in <namespace>?' "
        "or needs to discover configmap names + the set of keys each one "
        "exposes -- WITHOUT exposing values to the audit/broadcast feed. "
        "For full data, follow up with k8s.configmap.info per "
        "configmap (the targeted read records a per-configmap audit row)."
    ),
    "parameter_hints": {
        "namespace": ("Required. The Kubernetes namespace whose configmaps to list."),
    },
    "output_shape": (
        "{'rows': [{name, namespace, keys, age_seconds}], 'total': <int>}. "
        "``keys`` is the sorted union of ``data`` + ``binary_data`` keys. "
        "Values are NEVER included in this op; call k8s.configmap.info "
        "to read them."
    ),
}


K8S_CONFIGMAP_INFO_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "pattern": r"\S",
            "description": "Configmap name (exact match; no prefix resolution).",
        },
        "namespace": _NAMESPACE_PARAM_SCHEMA,
    },
    "required": ["name", "namespace"],
    "additionalProperties": False,
}


K8S_CONFIGMAP_INFO_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": ["string", "null"]},
        "namespace": {"type": ["string", "null"]},
        "data": {"type": "object"},
        "binary_data": {"type": "object"},
        "metadata": {
            "type": "object",
            "properties": {
                "labels": {"type": "object"},
                "annotations": {"type": "object"},
                "age_seconds": {"type": ["integer", "null"]},
            },
            "required": ["labels", "annotations", "age_seconds"],
            "additionalProperties": False,
        },
    },
    "required": ["name", "namespace", "data", "binary_data", "metadata"],
    "additionalProperties": False,
}


K8S_CONFIGMAP_INFO_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Call when the operator has named a specific configmap and "
        "needs to read its values (data + binary_data). The targeted "
        "read records a per-configmap audit row; prefer this over the "
        "list op when the operator's question is 'what is the value of "
        "key X in configmap Y?'. v0.2 audits as ``op_class=read``; "
        "G6.3 may upgrade ``*-secret-config``-named configmaps to "
        "``sensitive-read``."
    ),
    "parameter_hints": {
        "name": "Required. Exact configmap name (no prefix resolution).",
        "namespace": "Required. The Kubernetes namespace the configmap lives in.",
    },
    "output_shape": (
        "{'name', 'namespace', 'data': {<key>: <text-value>}, "
        "'binary_data': {<key>: <base64-string>}, 'metadata': "
        "{'labels', 'annotations', 'age_seconds'}}. ``binary_data`` "
        "values are forwarded base64-encoded (the K8s API's wire "
        "shape); the connector does not auto-decode."
    ),
}


CONFIG_OPS: tuple[KubernetesOp, ...] = (
    KubernetesOp(
        op_id="k8s.configmap.list",
        handler_attr="k8s_configmap_list",
        summary="List configmaps in a namespace -- keys only, NO values.",
        description=(
            "Calls ``CoreV1Api.list_namespaced_config_map(namespace)`` "
            "and projects each ConfigMap into {name, namespace, keys, "
            "age_seconds}. ``keys`` is the sorted union of ``data`` "
            "and ``binary_data`` keys. Values are **never** included -- "
            "the list op is the privacy-safe entry point; operators "
            "that need values call ``k8s.configmap.info`` per "
            "configmap so the audit row records the targeted read "
            "instead of the bulk dump. Read-only."
        ),
        parameter_schema=K8S_CONFIGMAP_LIST_PARAMETER_SCHEMA,
        response_schema=K8S_CONFIGMAP_LIST_RESPONSE_SCHEMA,
        group_key="config",
        tags=("read-only", "config", "configmap"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=K8S_CONFIGMAP_LIST_LLM_INSTRUCTIONS,
    ),
    KubernetesOp(
        op_id="k8s.configmap.info",
        handler_attr="k8s_configmap_info",
        summary="Read a single configmap with full data + binary_data.",
        description=(
            "Calls ``CoreV1Api.read_namespaced_config_map(name, namespace)`` "
            "and projects the result into {name, namespace, data, "
            "binary_data, metadata: {labels, annotations, age_seconds}}. "
            "``data`` carries the text values verbatim; ``binary_data`` "
            "values are forwarded base64-encoded (the K8s wire shape) "
            "without auto-decoding. The op is the targeted-read "
            "counterpart to ``k8s.configmap.list`` -- the audit row "
            "captures the configmap name so a post-incident query can "
            "answer 'who read which configmap when?'. v0.2 audits as "
            "``op_class=read``; G6.3 may upgrade specific patterns to "
            "``sensitive-read``. Read-only."
        ),
        parameter_schema=K8S_CONFIGMAP_INFO_PARAMETER_SCHEMA,
        response_schema=K8S_CONFIGMAP_INFO_RESPONSE_SCHEMA,
        group_key="config",
        tags=("read-only", "config", "configmap"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=K8S_CONFIGMAP_INFO_LLM_INSTRUCTIONS,
    ),
)
