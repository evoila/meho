# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Storage read ops -- StorageClass / PersistentVolume / PersistentVolumeClaim lists.

Connector read-op coverage wave 2 (#2830 of Initiative #2833). Closes
the pre-flight storage-sizing gap: before this tier the connector could
*count* PVCs (``k8s.ls``'s per-namespace walk) but returned no
capacity / storageclass / provisioner columns, and ``k8s.ls /`` already
advertised ``storageclasses`` / ``persistentvolumes`` in its
``cluster_kinds`` root output with no op behind them (dead-end
advertisements -- see
:data:`~meho_backplane.connectors.kubernetes.ops_core.K8S_CLUSTER_KINDS`).
The three ops here back those advertisements so an agent told the kinds
exist can actually read them.

* ``k8s.storageclass.list`` -- ``StorageV1Api.list_storage_class``.
  Cluster-scoped (no ``namespace`` axis), one row per StorageClass with
  ``{name, provisioner, is_default, reclaim_policy,
  volume_binding_mode, allow_expansion}``. ``is_default`` reads the GA
  ``storageclass.kubernetes.io/is-default-class`` annotation.
* ``k8s.persistentvolume.list`` -- ``CoreV1Api.list_persistent_volume``.
  Cluster-scoped (mirrors ``k8s.node.list``), one row per PV with
  ``{name, phase, capacity, storage_class, claim_ref, access_modes,
  reclaim_policy}``.
* ``k8s.persistentvolumeclaim.list`` -- per-namespace
  ``CoreV1Api.list_namespaced_persistent_volume_claim`` /
  cluster-wide ``CoreV1Api.list_persistent_volume_claim_for_all_namespaces``
  (the ``namespace`` XOR ``all_namespaces`` selector shared from
  :mod:`ops_listparams`). One row per PVC with ``{name, namespace,
  status, capacity, storage_class, volume_name, access_modes}``.

Both cluster-scoped list ops (``storageclass`` / ``persistentvolume``)
follow the ``k8s.node.list`` no-parameter shape rather than the
namespaced XOR shape -- StorageClasses and PersistentVolumes are
cluster-scoped resources with no namespace axis (authoring contract
branch 3 in :mod:`ops_listparams`). The PVC op deliberately omits the
``label_selector`` / ``limit`` / ``continue_token`` knobs the workload
list ops carry; the G0.17-T1 (#1330) paging deferral applies to this
new op unchanged (#2830 Out of scope).

Row-shape helpers (:func:`storageclass_row`, :func:`persistentvolume_row`,
:func:`persistentvolumeclaim_row`) are pure functions over
:mod:`kubernetes_asyncio.client.models` instances so the unit tests can
pin the wire shape against synthetic fixtures without booting an event
loop -- the same discipline the ops_core / ops_network helpers
established. The handlers live as bound methods on
:class:`~meho_backplane.connectors.kubernetes.connector.KubernetesConnector`
(``k8s_storageclass_list`` / ``k8s_persistentvolume_list`` /
``k8s_persistentvolumeclaim_list``) and delegate the model -> row
projection to the helpers here.

References
----------
* Parent task: #2830; parent Initiative: #2833 (read-op coverage wave 2).
* Request-shape parity: G0.17-T1 (#1330); conventions doc
  ``docs/codebase/api-shape-conventions.md`` §10.
* k8s StorageClass API: https://kubernetes.io/docs/reference/kubernetes-api/config-and-storage-resources/storage-class-v1/
* k8s PersistentVolume API: https://kubernetes.io/docs/reference/kubernetes-api/config-and-storage-resources/persistent-volume-v1/
* ``kubernetes_asyncio.StorageV1Api``:
  https://github.com/tomplus/kubernetes_asyncio/blob/master/kubernetes_asyncio/docs/StorageV1Api.md
* ``kubernetes_asyncio.CoreV1Api``:
  https://github.com/tomplus/kubernetes_asyncio/blob/master/kubernetes_asyncio/docs/CoreV1Api.md
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.kubernetes.ops import KubernetesOp
from meho_backplane.connectors.kubernetes.ops_listparams import (
    ALL_NAMESPACES_PARAM,
    NAMESPACE_PARAM,
    NAMESPACE_XOR_ALL_NAMESPACES,
)

if TYPE_CHECKING:
    from kubernetes_asyncio.client.models import (
        V1ObjectReference,
        V1PersistentVolume,
        V1PersistentVolumeClaim,
        V1StorageClass,
    )

__all__ = [
    "IS_DEFAULT_STORAGE_CLASS_ANNOTATION",
    "K8S_PERSISTENTVOLUMECLAIM_LIST_LLM_INSTRUCTIONS",
    "K8S_PERSISTENTVOLUMECLAIM_LIST_PARAMETER_SCHEMA",
    "K8S_PERSISTENTVOLUMECLAIM_LIST_RESPONSE_SCHEMA",
    "K8S_PERSISTENTVOLUME_LIST_LLM_INSTRUCTIONS",
    "K8S_PERSISTENTVOLUME_LIST_PARAMETER_SCHEMA",
    "K8S_PERSISTENTVOLUME_LIST_RESPONSE_SCHEMA",
    "K8S_STORAGECLASS_LIST_LLM_INSTRUCTIONS",
    "K8S_STORAGECLASS_LIST_PARAMETER_SCHEMA",
    "K8S_STORAGECLASS_LIST_RESPONSE_SCHEMA",
    "STORAGE_OPS",
    "persistentvolume_row",
    "persistentvolumeclaim_row",
    "storageclass_row",
]


#: GA annotation key marking a StorageClass as the cluster default. The
#: value is the literal string ``"true"`` (not a bool); the beta form
#: ``storageclass.beta.kubernetes.io/is-default-class`` predates k8s
#: 1.11 and is not read here -- every supported target sets the GA key.
IS_DEFAULT_STORAGE_CLASS_ANNOTATION = "storageclass.kubernetes.io/is-default-class"


# ---------------------------------------------------------------------------
# Row-shape helpers -- pure mappings over kubernetes_asyncio model objects.
# ---------------------------------------------------------------------------


def _storage_quantity(capacity: dict[str, str] | None) -> str | None:
    """Pluck the ``storage`` resource quantity from a capacity map.

    Both ``V1PersistentVolumeSpec.capacity`` and
    ``V1PersistentVolumeClaimStatus.capacity`` are ``ResourceList``
    dicts (``{"storage": "10Gi"}``). Operators sizing a cluster want the
    single ``storage`` quantity string, not the whole map; ``None`` when
    the capacity is unset (an unbound PVC has no ``status.capacity``).
    """
    if not capacity:
        return None
    return capacity.get("storage")


def _claim_ref(claim_ref: V1ObjectReference | None) -> str | None:
    """Render a PV's bound claim reference as ``namespace/name``.

    ``spec.claim_ref`` is a :class:`V1ObjectReference`; kubectl shows the
    bound PVC as ``<namespace>/<name>`` in the CLAIM column, which is the
    operator's mental model. ``None`` when the PV is unbound (Available /
    Released with the ref cleared); the name alone when the reference
    carries no namespace (defensive -- PVCs are always namespaced).
    """
    if claim_ref is None:
        return None
    namespace = claim_ref.namespace
    name = claim_ref.name
    if namespace and name:
        return f"{namespace}/{name}"
    return name


def storageclass_row(sc: V1StorageClass) -> dict[str, Any]:
    """Project a :class:`V1StorageClass` into the wire dict shape.

    ``is_default`` reads the GA
    ``storageclass.kubernetes.io/is-default-class`` annotation; the
    annotations block is ``None`` on a StorageClass with no annotations,
    so it is coerced to ``{}`` before the lookup. ``allow_expansion``
    forwards ``allow_volume_expansion`` verbatim (``None`` when the
    field is unset -- the provisioner's default applies).
    """
    metadata = sc.metadata
    annotations = (metadata.annotations or {}) if metadata is not None else {}
    return {
        "name": metadata.name if metadata is not None else None,
        "provisioner": sc.provisioner,
        "is_default": annotations.get(IS_DEFAULT_STORAGE_CLASS_ANNOTATION) == "true",
        "reclaim_policy": sc.reclaim_policy,
        "volume_binding_mode": sc.volume_binding_mode,
        "allow_expansion": sc.allow_volume_expansion,
    }


def persistentvolume_row(pv: V1PersistentVolume) -> dict[str, Any]:
    """Project a :class:`V1PersistentVolume` into the wire dict shape.

    ``phase`` is the lifecycle phase from ``status`` (Available / Bound /
    Released / Failed). ``capacity`` is the ``storage`` quantity from
    ``spec.capacity``. ``claim_ref`` is the bound PVC rendered as
    ``namespace/name`` (``None`` when unbound). ``reclaim_policy`` is the
    ``persistentVolumeReclaimPolicy`` (Retain / Delete / Recycle).
    """
    metadata = pv.metadata
    spec = pv.spec
    status = pv.status
    return {
        "name": metadata.name if metadata is not None else None,
        "phase": status.phase if status is not None else None,
        "capacity": _storage_quantity(spec.capacity if spec is not None else None),
        "storage_class": spec.storage_class_name if spec is not None else None,
        "claim_ref": _claim_ref(spec.claim_ref if spec is not None else None),
        "access_modes": (list(spec.access_modes or []) if spec is not None else []),
        "reclaim_policy": (spec.persistent_volume_reclaim_policy if spec is not None else None),
    }


def persistentvolumeclaim_row(pvc: V1PersistentVolumeClaim) -> dict[str, Any]:
    """Project a :class:`V1PersistentVolumeClaim` into the wire dict shape.

    ``status`` is the PVC phase (Pending / Bound / Lost). ``capacity`` is
    the ``storage`` quantity from ``status.capacity`` -- the *bound*
    volume size (kubectl's CAPACITY column), ``None`` for an unbound
    (Pending) claim whose status carries no capacity yet. ``storage_class``
    /``volume_name`` come from ``spec``; ``volume_name`` is the bound PV
    (``None`` until binding completes).
    """
    metadata = pvc.metadata
    spec = pvc.spec
    status = pvc.status
    return {
        "name": metadata.name if metadata is not None else None,
        "namespace": metadata.namespace if metadata is not None else None,
        "status": status.phase if status is not None else None,
        "capacity": _storage_quantity(status.capacity if status is not None else None),
        "storage_class": spec.storage_class_name if spec is not None else None,
        "volume_name": spec.volume_name if spec is not None else None,
        "access_modes": (list(spec.access_modes or []) if spec is not None else []),
    }


# ---------------------------------------------------------------------------
# Op metadata -- schemas + llm_instructions + KubernetesOp rows.
# ---------------------------------------------------------------------------


#: ``k8s.storageclass.list`` takes no parameters -- StorageClasses are
#: cluster-scoped (no namespace axis), same shape as ``k8s.node.list``.
K8S_STORAGECLASS_LIST_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


K8S_STORAGECLASS_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": ["string", "null"]},
                    "provisioner": {"type": ["string", "null"]},
                    "is_default": {"type": "boolean"},
                    "reclaim_policy": {"type": ["string", "null"]},
                    "volume_binding_mode": {"type": ["string", "null"]},
                    "allow_expansion": {"type": ["boolean", "null"]},
                },
                "required": [
                    "name",
                    "provisioner",
                    "is_default",
                    "reclaim_policy",
                    "volume_binding_mode",
                    "allow_expansion",
                ],
                "additionalProperties": False,
            },
        },
        "total": {"type": "integer"},
    },
    "required": ["rows", "total"],
    "additionalProperties": False,
}


K8S_STORAGECLASS_LIST_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Call for pre-flight storage sizing: 'which StorageClass is the "
        "default?', 'what provisioner backs the fast tier?', 'can this "
        "class expand volumes?'. Read-only; cluster-scoped (no "
        "namespace). Pair with ``k8s.persistentvolume.list`` for the "
        "provisioned-volume side and ``k8s.persistentvolumeclaim.list`` "
        "for what workloads have requested."
    ),
    "parameter_hints": {},
    "output_shape": (
        "{'rows': [{name, provisioner, is_default, reclaim_policy, "
        "volume_binding_mode, allow_expansion}], 'total': <int>}. "
        "``is_default`` is the boolean from the "
        "``storageclass.kubernetes.io/is-default-class`` annotation; "
        "``volume_binding_mode`` is 'Immediate' or "
        "'WaitForFirstConsumer'; ``allow_expansion`` may be null when "
        "the class leaves it unset."
    ),
}


#: ``k8s.persistentvolume.list`` takes no parameters -- PersistentVolumes
#: are cluster-scoped (no namespace axis), same shape as ``k8s.node.list``.
K8S_PERSISTENTVOLUME_LIST_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


K8S_PERSISTENTVOLUME_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": ["string", "null"]},
                    "phase": {"type": ["string", "null"]},
                    "capacity": {"type": ["string", "null"]},
                    "storage_class": {"type": ["string", "null"]},
                    "claim_ref": {"type": ["string", "null"]},
                    "access_modes": {"type": "array", "items": {"type": "string"}},
                    "reclaim_policy": {"type": ["string", "null"]},
                },
                "required": [
                    "name",
                    "phase",
                    "capacity",
                    "storage_class",
                    "claim_ref",
                    "access_modes",
                    "reclaim_policy",
                ],
                "additionalProperties": False,
            },
        },
        "total": {"type": "integer"},
    },
    "required": ["rows", "total"],
    "additionalProperties": False,
}


K8S_PERSISTENTVOLUME_LIST_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Call to inventory cluster-provisioned storage: 'which PVs "
        "exist and what backs them?', 'is this PV bound or released?', "
        "'how much capacity is provisioned?'. Read-only; cluster-scoped "
        "(no namespace). Pair with ``k8s.persistentvolumeclaim.list`` "
        "to map a PV back to the claim that bound it."
    ),
    "parameter_hints": {},
    "output_shape": (
        "{'rows': [{name, phase, capacity, storage_class, claim_ref, "
        "access_modes, reclaim_policy}], 'total': <int>}. ``phase`` is "
        "'Available' / 'Bound' / 'Released' / 'Failed'; ``capacity`` is "
        "the storage quantity string (e.g. '10Gi'); ``claim_ref`` is "
        "the bound PVC as 'namespace/name' (null when unbound)."
    ),
}


#: ``k8s.persistentvolumeclaim.list`` adopts the shared ``namespace`` XOR
#: ``all_namespaces`` selector (G0.17-T1 #1330). ``label_selector`` /
#: server-side paging are deliberately omitted -- the #1330 deferral
#: applies to this new op unchanged (#2830 Out of scope).
K8S_PERSISTENTVOLUMECLAIM_LIST_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "namespace": NAMESPACE_PARAM,
        "all_namespaces": ALL_NAMESPACES_PARAM,
    },
    "oneOf": NAMESPACE_XOR_ALL_NAMESPACES,
    "additionalProperties": False,
}


K8S_PERSISTENTVOLUMECLAIM_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": ["string", "null"]},
                    "namespace": {"type": ["string", "null"]},
                    "status": {"type": ["string", "null"]},
                    "capacity": {"type": ["string", "null"]},
                    "storage_class": {"type": ["string", "null"]},
                    "volume_name": {"type": ["string", "null"]},
                    "access_modes": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "name",
                    "namespace",
                    "status",
                    "capacity",
                    "storage_class",
                    "volume_name",
                    "access_modes",
                ],
                "additionalProperties": False,
            },
        },
        "total": {"type": "integer"},
    },
    "required": ["rows", "total"],
    "additionalProperties": False,
}


K8S_PERSISTENTVOLUMECLAIM_LIST_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Call to see what storage workloads have requested: 'which PVCs "
        "are Pending (unbound)?', 'how much has namespace X claimed?', "
        "'which StorageClass does this claim use?'. Use "
        "``all_namespaces=true`` for a cluster-wide sizing view. "
        "Read-only. Pair with ``k8s.persistentvolume.list`` for the "
        "provisioned-volume side and ``k8s.storageclass.list`` for the "
        "class definitions."
    ),
    "parameter_hints": {
        "namespace": "Required unless ``all_namespaces`` is true.",
        "all_namespaces": (
            "Pass true for a cluster-wide PVC listing; mutually exclusive with ``namespace``."
        ),
    },
    "output_shape": (
        "{'rows': [{name, namespace, status, capacity, storage_class, "
        "volume_name, access_modes}], 'total': <int>}. ``status`` is "
        "'Pending' / 'Bound' / 'Lost'; ``capacity`` is the bound "
        "storage quantity (null while Pending); ``volume_name`` is the "
        "bound PV (null until binding completes). Each row carries its "
        "own ``namespace`` so cross-namespace rows stay distinguishable."
    ),
}


STORAGE_OPS: tuple[KubernetesOp, ...] = (
    KubernetesOp(
        op_id="k8s.storageclass.list",
        handler_attr="k8s_storageclass_list",
        summary="List StorageClasses -- provisioner / default / expansion / binding mode.",
        description=(
            "Calls ``StorageV1Api.list_storage_class()`` and projects "
            "each StorageClass into {name, provisioner, is_default, "
            "reclaim_policy, volume_binding_mode, allow_expansion}. "
            "``is_default`` is derived from the GA "
            "``storageclass.kubernetes.io/is-default-class`` annotation "
            "(the annotations block is guarded for None). Cluster-scoped "
            "(no namespace parameter). Read-only; the pre-flight "
            "'which class is default and what backs it?' question."
        ),
        parameter_schema=K8S_STORAGECLASS_LIST_PARAMETER_SCHEMA,
        response_schema=K8S_STORAGECLASS_LIST_RESPONSE_SCHEMA,
        group_key="storage",
        tags=("read-only", "storage", "storageclass"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=K8S_STORAGECLASS_LIST_LLM_INSTRUCTIONS,
    ),
    KubernetesOp(
        op_id="k8s.persistentvolume.list",
        handler_attr="k8s_persistentvolume_list",
        summary="List PersistentVolumes -- phase / capacity / storage class / claim.",
        description=(
            "Calls ``CoreV1Api.list_persistent_volume()`` and projects "
            "each PV into {name, phase, capacity, storage_class, "
            "claim_ref, access_modes, reclaim_policy}. ``phase`` is the "
            "lifecycle state (Available / Bound / Released / Failed); "
            "``capacity`` is the storage quantity string; ``claim_ref`` "
            "is the bound PVC rendered as 'namespace/name' (None when "
            "unbound). Cluster-scoped (no namespace parameter, mirrors "
            "``k8s.node.list``). Read-only."
        ),
        parameter_schema=K8S_PERSISTENTVOLUME_LIST_PARAMETER_SCHEMA,
        response_schema=K8S_PERSISTENTVOLUME_LIST_RESPONSE_SCHEMA,
        group_key="storage",
        tags=("read-only", "storage", "persistentvolume"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=K8S_PERSISTENTVOLUME_LIST_LLM_INSTRUCTIONS,
    ),
    KubernetesOp(
        op_id="k8s.persistentvolumeclaim.list",
        handler_attr="k8s_persistentvolumeclaim_list",
        summary="List PersistentVolumeClaims -- status / capacity / storage class.",
        description=(
            "Calls "
            "``CoreV1Api.list_namespaced_persistent_volume_claim(namespace, ...)`` "
            "(per-namespace) or "
            "``CoreV1Api.list_persistent_volume_claim_for_all_namespaces(...)`` "
            "(``all_namespaces=true``) and projects each PVC into {name, "
            "namespace, status, capacity, storage_class, volume_name, "
            "access_modes}. ``status`` is the PVC phase (Pending / Bound "
            "/ Lost); ``capacity`` is the bound storage quantity from "
            "``status.capacity`` (None while Pending); ``volume_name`` "
            "is the bound PV. Read-only. Op id is "
            "``k8s.persistentvolumeclaim.list`` (not ``pvc``) so "
            "``k8s.ls /<ns>/persistentvolumeclaims`` forwards to it."
        ),
        parameter_schema=K8S_PERSISTENTVOLUMECLAIM_LIST_PARAMETER_SCHEMA,
        response_schema=K8S_PERSISTENTVOLUMECLAIM_LIST_RESPONSE_SCHEMA,
        group_key="storage",
        tags=("read-only", "storage", "persistentvolumeclaim"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=K8S_PERSISTENTVOLUMECLAIM_LIST_LLM_INSTRUCTIONS,
    ),
)
