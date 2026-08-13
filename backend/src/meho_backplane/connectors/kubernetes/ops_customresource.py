# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Custom-resource read ops -- ``k8s.crd.list`` / ``k8s.cr.list`` / ``k8s.cr.info``.

Connector read-op coverage wave 2 (#2830 of Initiative #2833). On
GitOps-heavy clusters the operationally interesting state lives in
custom resources (MetalLB ``IPAddressPool``, cert-manager
``Certificate``, ESO ``ExternalSecret``, Argo / Flux) -- and while
``k8s.apply`` could already *write* a CR through the dynamic client, no
op could *read* one back. These three ops close that gap generically,
without a per-CRD typed op:

* ``k8s.crd.list`` -- ``ApiextensionsV1Api.list_custom_resource_definition``.
  One row per CRD with ``{group, kind, plural, scope, versions}`` so the
  agent can discover the ``group`` / ``version`` / ``plural`` triple a
  ``k8s.cr.*`` call needs.
* ``k8s.cr.list`` -- generic list over ``CustomObjectsApi``:
  ``list_namespaced_custom_object`` when a ``namespace`` is supplied,
  ``list_cluster_custom_object`` otherwise (which lists a namespaced CRD
  across every namespace, or the objects of a cluster-scoped CRD).
* ``k8s.cr.info`` -- generic single-object read:
  ``get_namespaced_custom_object`` / ``get_cluster_custom_object``.

CustomObjectsApi returns **plain dicts** (custom resources have no
generated typed model), so the CR projection walks dict keys rather than
model attributes. Every CR object is projected through
:func:`custom_resource_row`, which keeps the result **bounded**: the
operator-relevant metadata identifiers plus a JSON-serialised, byte-
capped excerpt of ``.spec`` (:data:`CR_SPEC_EXCERPT_MAX_BYTES`). A large
Argo ``Application`` or cert-manager ``Certificate`` spec cannot blow
the result envelope; when the excerpt is truncated, ``spec_truncated``
is ``true`` and the excerpt is a preview (not parseable JSON). The
truncation rule is documented in each op's ``llm_instructions``. The
verbose ``metadata.managedFields`` / ``metadata.annotations`` blocks
(server-side-apply carries huge ones) are dropped from the projection
entirely.

Helpers (:func:`crd_row`, :func:`crd_version_row`,
:func:`custom_resource_row`) are pure functions so the unit tests pin
the wire shape -- including the truncation bound -- without an event
loop. The handlers live as bound methods on
:class:`~meho_backplane.connectors.kubernetes.connector.KubernetesConnector`
(``k8s_crd_list`` / ``k8s_cr_list`` / ``k8s_cr_info``).

References
----------
* Parent task: #2830; parent Initiative: #2833 (read-op coverage wave 2).
* Prior art: ``k8s.apply`` already drives the dynamic client for CR
  writes (:mod:`ops_write_dangerous`); this is the read counterpart.
* k8s CRD API: https://kubernetes.io/docs/reference/kubernetes-api/extend-resources/custom-resource-definition-v1/
* ``kubernetes_asyncio.ApiextensionsV1Api``:
  https://github.com/tomplus/kubernetes_asyncio/blob/master/kubernetes_asyncio/docs/ApiextensionsV1Api.md
* ``kubernetes_asyncio.CustomObjectsApi``:
  https://github.com/tomplus/kubernetes_asyncio/blob/master/kubernetes_asyncio/docs/CustomObjectsApi.md
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.kubernetes.ops import KubernetesOp

if TYPE_CHECKING:
    from kubernetes_asyncio.client.models import (
        V1CustomResourceDefinition,
        V1CustomResourceDefinitionVersion,
    )

__all__ = [
    "CR_SPEC_EXCERPT_MAX_BYTES",
    "CUSTOM_RESOURCE_OPS",
    "K8S_CRD_LIST_LLM_INSTRUCTIONS",
    "K8S_CRD_LIST_PARAMETER_SCHEMA",
    "K8S_CRD_LIST_RESPONSE_SCHEMA",
    "K8S_CR_INFO_LLM_INSTRUCTIONS",
    "K8S_CR_INFO_PARAMETER_SCHEMA",
    "K8S_CR_INFO_RESPONSE_SCHEMA",
    "K8S_CR_LIST_LLM_INSTRUCTIONS",
    "K8S_CR_LIST_PARAMETER_SCHEMA",
    "K8S_CR_LIST_RESPONSE_SCHEMA",
    "crd_row",
    "crd_version_row",
    "custom_resource_row",
]


#: Byte budget for the JSON-serialised ``.spec`` excerpt on a CR row.
#: Bounds ``k8s.cr.list`` / ``k8s.cr.info`` result sizes so a large CR
#: spec (Argo Application, cert-manager Certificate) cannot blow the
#: envelope. A pre-flight ``IPAddressPool`` spec is well under this, so
#: the target sizing use case never truncates.
CR_SPEC_EXCERPT_MAX_BYTES = 2048


# ---------------------------------------------------------------------------
# Row-shape helpers -- pure mappings over kubernetes_asyncio models (CRD)
# and over the plain dicts CustomObjectsApi returns (CR).
# ---------------------------------------------------------------------------


def crd_version_row(version: V1CustomResourceDefinitionVersion) -> dict[str, Any]:
    """Project a :class:`V1CustomResourceDefinitionVersion` to {name, served, storage}.

    ``served`` marks the version as reachable via the API; ``storage``
    marks the one version persisted in etcd (exactly one per CRD). The
    schema / subresources blocks are deliberately omitted -- the agent
    needs the ``name`` to build a ``k8s.cr.*`` ``version`` argument, not
    the full OpenAPI schema.
    """
    return {
        "name": version.name,
        "served": version.served,
        "storage": version.storage,
    }


def crd_row(crd: V1CustomResourceDefinition) -> dict[str, Any]:
    """Project a :class:`V1CustomResourceDefinition` into the wire dict shape.

    ``group`` / ``plural`` are exactly the arguments a follow-up
    ``k8s.cr.list`` / ``k8s.cr.info`` call needs; ``versions`` supplies
    the ``version`` argument (pick a ``served`` one, usually the
    ``storage`` version). ``scope`` is 'Namespaced' or 'Cluster' -- it
    tells the agent whether to pass a ``namespace`` to the CR read.
    """
    spec = crd.spec
    names = spec.names if spec is not None else None
    versions = spec.versions if spec is not None else None
    return {
        "group": spec.group if spec is not None else None,
        "kind": names.kind if names is not None else None,
        "plural": names.plural if names is not None else None,
        "scope": spec.scope if spec is not None else None,
        "versions": [crd_version_row(v) for v in (versions or [])],
    }


def _bounded_spec_excerpt(spec: Any) -> tuple[str | None, bool]:
    """Return ``(excerpt, truncated)`` for a CR's ``.spec`` block.

    ``spec`` is the raw value CustomObjectsApi hands back (typically a
    dict, but any JSON value is possible). It is serialised
    deterministically (``sort_keys``) and capped at
    :data:`CR_SPEC_EXCERPT_MAX_BYTES`. When the serialised form fits, the
    full spec round-trips as JSON; when it does not, the excerpt is
    truncated on a UTF-8 byte boundary and ``truncated`` is ``True`` (the
    excerpt is then a preview, not parseable JSON). ``(None, False)``
    when the object has no ``spec`` (e.g. a status-only CR).
    """
    if spec is None:
        return None, False
    serialised = json.dumps(spec, sort_keys=True, default=str)
    encoded = serialised.encode("utf-8")
    if len(encoded) <= CR_SPEC_EXCERPT_MAX_BYTES:
        return serialised, False
    # ``errors="ignore"`` drops a partial multi-byte sequence at the cut
    # point so the preview is always valid UTF-8.
    return encoded[:CR_SPEC_EXCERPT_MAX_BYTES].decode("utf-8", errors="ignore"), True


def custom_resource_row(obj: dict[str, Any]) -> dict[str, Any]:
    """Project a raw custom-resource dict into the bounded wire shape.

    Custom resources have no generated typed model, so *obj* is the
    plain dict CustomObjectsApi returns. The projection keeps the
    operator-relevant identifiers (name / namespace / apiVersion / kind /
    creationTimestamp / labels) and a bounded ``.spec`` excerpt (see
    :func:`_bounded_spec_excerpt`); the verbose ``managedFields`` /
    ``annotations`` metadata blocks are dropped to keep result sizes
    sane. ``namespace`` is ``None`` for a cluster-scoped CR.
    """
    metadata = obj.get("metadata") or {}
    excerpt, truncated = _bounded_spec_excerpt(obj.get("spec"))
    return {
        "name": metadata.get("name"),
        "namespace": metadata.get("namespace"),
        "api_version": obj.get("apiVersion"),
        "kind": obj.get("kind"),
        "creation_timestamp": metadata.get("creationTimestamp"),
        "labels": metadata.get("labels") or {},
        "spec_excerpt": excerpt,
        "spec_truncated": truncated,
    }


# ---------------------------------------------------------------------------
# Op metadata -- shared param blocks, schemas, llm_instructions, rows.
# ---------------------------------------------------------------------------


_GROUP_PARAM: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": (
        "API group of the custom resource (e.g. "
        "``metallb.io``, ``cert-manager.io``). From ``k8s.crd.list``'s "
        "``group`` column."
    ),
}

_VERSION_PARAM: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Served API version (e.g. ``v1beta1``, ``v1``). From a "
        "``k8s.crd.list`` row's ``versions[].name`` -- prefer the "
        "``storage`` version."
    ),
}

_PLURAL_PARAM: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Plural resource name (e.g. ``ipaddresspools``, "
        "``certificates``). From ``k8s.crd.list``'s ``plural`` column -- "
        "NOT the kind."
    ),
}

_CR_NAMESPACE_PARAM: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Namespace to scope to. Omit for a cluster-scoped CRD, or to "
        "list a namespaced CRD across every namespace."
    ),
}

_CR_NAME_PARAM: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": "Name of the specific custom-resource object to read.",
}


K8S_CRD_LIST_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


K8S_CRD_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "group": {"type": ["string", "null"]},
                    "kind": {"type": ["string", "null"]},
                    "plural": {"type": ["string", "null"]},
                    "scope": {"type": ["string", "null"]},
                    "versions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": ["string", "null"]},
                                "served": {"type": ["boolean", "null"]},
                                "storage": {"type": ["boolean", "null"]},
                            },
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["group", "kind", "plural", "scope", "versions"],
                "additionalProperties": False,
            },
        },
        "total": {"type": "integer"},
    },
    "required": ["rows", "total"],
    "additionalProperties": False,
}


K8S_CRD_LIST_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Call to discover which custom resources a cluster defines "
        "before reading any: 'what CRDs are installed?', 'what's the "
        "group/version/plural for MetalLB IPAddressPools?'. Read-only; "
        "cluster-scoped. The ``group`` / ``versions[].name`` / "
        "``plural`` columns are exactly the arguments a follow-up "
        "``k8s.cr.list`` / ``k8s.cr.info`` needs; ``scope`` tells you "
        "whether to pass a namespace."
    ),
    "parameter_hints": {},
    "output_shape": (
        "{'rows': [{group, kind, plural, scope, versions: [{name, "
        "served, storage}]}], 'total': <int>}. ``scope`` is "
        "'Namespaced' or 'Cluster'. Pick a ``served`` version (usually "
        "the ``storage`` one) for the ``k8s.cr.*`` ``version`` argument."
    ),
}


K8S_CR_LIST_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "group": _GROUP_PARAM,
        "version": _VERSION_PARAM,
        "plural": _PLURAL_PARAM,
        "namespace": _CR_NAMESPACE_PARAM,
    },
    "required": ["group", "version", "plural"],
    "additionalProperties": False,
}


_CR_ROW_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": ["string", "null"]},
        "namespace": {"type": ["string", "null"]},
        "api_version": {"type": ["string", "null"]},
        "kind": {"type": ["string", "null"]},
        "creation_timestamp": {"type": ["string", "null"]},
        "labels": {"type": "object"},
        "spec_excerpt": {"type": ["string", "null"]},
        "spec_truncated": {"type": "boolean"},
    },
    "required": [
        "name",
        "namespace",
        "api_version",
        "kind",
        "creation_timestamp",
        "labels",
        "spec_excerpt",
        "spec_truncated",
    ],
    "additionalProperties": False,
}


K8S_CR_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": {"type": "array", "items": _CR_ROW_ITEM_SCHEMA},
        "total": {"type": "integer"},
    },
    "required": ["rows", "total"],
    "additionalProperties": False,
}


K8S_CR_LIST_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Call to read custom-resource objects generically: 'which "
        "MetalLB IPAddressPools are allocated?', 'what cert-manager "
        "Certificates exist?'. Get ``group`` / ``version`` / ``plural`` "
        "from ``k8s.crd.list`` first. Omit ``namespace`` to list a "
        "namespaced CRD across every namespace (or for a cluster-scoped "
        "CRD); pass ``namespace`` to scope. Read-only."
    ),
    "parameter_hints": {
        "group": "API group, e.g. ``metallb.io``. From k8s.crd.list.",
        "version": "Served version, e.g. ``v1beta1``. From k8s.crd.list.",
        "plural": "Plural name, e.g. ``ipaddresspools``. NOT the kind.",
        "namespace": "Optional; omit for cluster-scoped or all-namespaces listing.",
    },
    "output_shape": (
        "{'rows': [{name, namespace, api_version, kind, "
        "creation_timestamp, labels, spec_excerpt, spec_truncated}], "
        "'total': <int>}. ``spec_excerpt`` is a JSON string of the CR's "
        f".spec capped at {CR_SPEC_EXCERPT_MAX_BYTES} bytes; when "
        "``spec_truncated`` is true the excerpt is a preview (not "
        "parseable JSON) -- narrow to one object via ``k8s.cr.info`` for "
        "that name."
    ),
}


K8S_CR_INFO_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "group": _GROUP_PARAM,
        "version": _VERSION_PARAM,
        "plural": _PLURAL_PARAM,
        "name": _CR_NAME_PARAM,
        "namespace": _CR_NAMESPACE_PARAM,
    },
    "required": ["group", "version", "plural", "name"],
    "additionalProperties": False,
}


K8S_CR_INFO_RESPONSE_SCHEMA: dict[str, Any] = _CR_ROW_ITEM_SCHEMA


K8S_CR_INFO_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Call to read one named custom-resource object: 'what is the "
        "address range on the metallb-system default IPAddressPool?'. "
        "Get ``group`` / ``version`` / ``plural`` from ``k8s.crd.list``; "
        "pass ``namespace`` for a namespaced CRD, omit it for a "
        "cluster-scoped one. Read-only."
    ),
    "parameter_hints": {
        "group": "API group, e.g. ``metallb.io``. From k8s.crd.list.",
        "version": "Served version, e.g. ``v1beta1``. From k8s.crd.list.",
        "plural": "Plural name, e.g. ``ipaddresspools``. NOT the kind.",
        "name": "The object's metadata.name.",
        "namespace": "Required for a namespaced CRD; omit for cluster-scoped.",
    },
    "output_shape": (
        "Single object (not a rows/total envelope): {name, namespace, "
        "api_version, kind, creation_timestamp, labels, spec_excerpt, "
        f"spec_truncated}}. ``spec_excerpt`` is the JSON .spec capped at "
        f"{CR_SPEC_EXCERPT_MAX_BYTES} bytes; ``spec_truncated`` flags a "
        "preview-only excerpt for a spec over the cap."
    ),
}


CUSTOM_RESOURCE_OPS: tuple[KubernetesOp, ...] = (
    KubernetesOp(
        op_id="k8s.crd.list",
        handler_attr="k8s_crd_list",
        summary="List CustomResourceDefinitions -- group / kind / plural / scope / versions.",
        description=(
            "Calls "
            "``ApiextensionsV1Api.list_custom_resource_definition()`` "
            "and projects each CRD into {group, kind, plural, scope, "
            "versions:[{name, served, storage}]}. The discovery op for "
            "the generic ``k8s.cr.*`` reads -- its group / version / "
            "plural columns are exactly the arguments those ops need. "
            "``scope`` ('Namespaced' / 'Cluster') tells the agent "
            "whether a namespace applies. Cluster-scoped; read-only."
        ),
        parameter_schema=K8S_CRD_LIST_PARAMETER_SCHEMA,
        response_schema=K8S_CRD_LIST_RESPONSE_SCHEMA,
        group_key="custom_resources",
        tags=("read-only", "custom-resource", "crd"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=K8S_CRD_LIST_LLM_INSTRUCTIONS,
    ),
    KubernetesOp(
        op_id="k8s.cr.list",
        handler_attr="k8s_cr_list",
        summary="List custom-resource objects by group/version/plural -- bounded spec excerpts.",
        description=(
            "Generic custom-resource list over ``CustomObjectsApi``: "
            "``list_namespaced_custom_object(group, version, namespace, "
            "plural)`` when a namespace is supplied, else "
            "``list_cluster_custom_object(group, version, plural)`` "
            "(which lists a namespaced CRD across all namespaces, or a "
            "cluster-scoped CRD's objects). Each object projects to "
            "{name, namespace, api_version, kind, creation_timestamp, "
            "labels, spec_excerpt, spec_truncated}. ``spec_excerpt`` is "
            "a JSON string of .spec capped at "
            f"{CR_SPEC_EXCERPT_MAX_BYTES} bytes; ``spec_truncated`` "
            "flags an over-cap preview. Read-only. Get group / version "
            "/ plural from ``k8s.crd.list``."
        ),
        parameter_schema=K8S_CR_LIST_PARAMETER_SCHEMA,
        response_schema=K8S_CR_LIST_RESPONSE_SCHEMA,
        group_key="custom_resources",
        tags=("read-only", "custom-resource", "cr"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=K8S_CR_LIST_LLM_INSTRUCTIONS,
    ),
    KubernetesOp(
        op_id="k8s.cr.info",
        handler_attr="k8s_cr_info",
        summary="Read one named custom-resource object -- bounded spec excerpt.",
        description=(
            "Generic single custom-resource read over "
            "``CustomObjectsApi``: "
            "``get_namespaced_custom_object(group, version, namespace, "
            "plural, name)`` when a namespace is supplied, else "
            "``get_cluster_custom_object(group, version, plural, name)``. "
            "Returns the single-object projection {name, namespace, "
            "api_version, kind, creation_timestamp, labels, "
            "spec_excerpt, spec_truncated} (not a rows/total envelope). "
            "``spec_excerpt`` is the JSON .spec capped at "
            f"{CR_SPEC_EXCERPT_MAX_BYTES} bytes. Read-only; the "
            "counterpart drill-in for a ``k8s.cr.list`` row."
        ),
        parameter_schema=K8S_CR_INFO_PARAMETER_SCHEMA,
        response_schema=K8S_CR_INFO_RESPONSE_SCHEMA,
        group_key="custom_resources",
        tags=("read-only", "custom-resource", "cr"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=K8S_CR_INFO_LLM_INSTRUCTIONS,
    ),
)
