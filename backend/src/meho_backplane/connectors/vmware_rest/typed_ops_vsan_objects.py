# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""vSAN object reads: ``vmware.vsan.resync.status`` and ``vmware.vsan.object.health``.

Both use the vSAN object service of vCenter, ``VsanObjectSystem`` (object
``vsan-cluster-object-system``), on the connector's own session (VI-JSON on
vCenter 9.x, SOAP on 8.0, see :meth:`VmwareRestConnector._post_vsan`).

``vmware.vsan.resync.status`` sends ``QuerySyncingVsanObjectsSummary`` with a
``VsanSyncingObjectFilter``: the totals (objects, bytes and time left) for
the active, queued and suspended resyncs, and up to ``max_objects`` syncing
objects with each component, its bytes and time left, and why it resyncs.

``vmware.vsan.object.health`` sends two calls:

1. ``VsanVcClusterHealthSystem.VsanQueryVcClusterHealthSummary`` with
   ``fields: ["objectHealth"]`` and ``includeObjUuids: true``: the number of
   objects in each health state, and their UUIDs.
2. When some objects are not healthy: ``VsanObjectSystem.
   VsanQueryObjectIdentities`` for those UUIDs, with ``includeHealth`` and
   ``includeObjIdentity``: the type of each object and the VM it belongs to.

Field names: the vendor VI/JSON file of vCenter 9.0
(``VsanHostVsanObjectSyncQueryResult``, ``VsanObjectOverallHealth``,
``VsanObjectIdentityAndHealth``, ``VsanObjectHealthState_enum``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import structlog

from meho_backplane.connectors.vmware_rest._mount import VsanMethod
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp
from meho_backplane.connectors.vmware_rest.typed_ops_host_vsan_health import (
    VSAN_QUERY_HEALTH_SUMMARY,
)
from meho_backplane.connectors.vmware_rest.typed_ops_vsan_common import (
    CLUSTER_PARAM_SCHEMA,
    FROM_CACHE_PARAM_SCHEMA,
    VSAN_GROUP_KEY,
    as_int,
    as_text,
    cluster_ref,
    dict_items,
    health_summary_body,
    text_items,
    vsan_call,
)
from meho_backplane.connectors.vmware_rest.typed_ops_vsan_objects_schemas import (
    OBJECT_HEALTH_LLM_INSTRUCTIONS,
    OBJECT_HEALTH_RESPONSE_SCHEMA,
    RESYNC_LLM_INSTRUCTIONS,
    RESYNC_PARAMETER_SCHEMA,
    RESYNC_RESPONSE_SCHEMA,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike

__all__ = [
    "HEALTH_GROUP_HEALTHY",
    "VMWARE_VSAN_OBJECT_HEALTH_OP",
    "VMWARE_VSAN_RESYNC_STATUS_OP",
    "VSAN_QUERY_OBJECT_IDENTITIES",
    "VSAN_QUERY_SYNCING_OBJECTS",
    "health_group",
    "vsan_object_health_impl",
    "vsan_resync_status_impl",
]

_OBJECT_SYSTEM: Final = "VsanObjectSystem"
_OBJECT_SYSTEM_MOID: Final = "vsan-cluster-object-system"

VSAN_QUERY_SYNCING_OBJECTS: Final = VsanMethod(
    mo_type=_OBJECT_SYSTEM, moid=_OBJECT_SYSTEM_MOID, method="QuerySyncingVsanObjectsSummary"
)
VSAN_QUERY_OBJECT_IDENTITIES: Final = VsanMethod(
    mo_type=_OBJECT_SYSTEM, moid=_OBJECT_SYSTEM_MOID, method="VsanQueryObjectIdentities"
)

_DEFAULT_MAX_OBJECTS: Final = 100
#: ``VsanSyncingObjectRecoveryDetails`` fields per resync status: objects, bytes, seconds.
_SYNC_DETAIL_KEYS: Final[dict[str, tuple[str, str, str]]] = {
    "active": (
        "activeObjectsToSync",
        "bytesToSyncForActiveObjects",
        "activelySyncingObjectRecoveryETA",
    ),
    "queued": (
        "queuedObjectsToSync",
        "bytesToSyncForQueuedObjects",
        "queuedForSyncObjectRecoveryETA",
    ),
    "suspended": (
        "suspendedObjectsToSync",
        "bytesToSyncForSuspendedObjects",
        "suspendedObjectRecoveryETA",
    ),
}

#: Plain groups of the vSAN object health states (``VsanObjectHealthState_enum``).
HEALTH_GROUP_HEALTHY: Final = "healthy"
HEALTH_GROUP_REDUCED: Final = "reduced_redundancy"
HEALTH_GROUP_INACCESSIBLE: Final = "inaccessible"
HEALTH_GROUP_OTHER: Final = "other"


def _log() -> Any:
    """Get the logger per call, so ``structlog.testing.capture_logs`` sees every event."""
    return structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# vmware.vsan.resync.status
# ---------------------------------------------------------------------------


def build_syncing_filter_body(cluster_moid: str, params: dict[str, Any]) -> dict[str, Any]:
    """The ``QuerySyncingVsanObjectsSummary`` body, keys in the order the vendor types define."""
    sync_filter: dict[str, Any] = {"_typeName": "VsanSyncingObjectFilter"}
    if params.get("reason"):
        sync_filter["resyncType"] = params["reason"]
    if params.get("status"):
        sync_filter["resyncStatus"] = params["status"]
    sync_filter["numberOfObjects"] = int(params.get("max_objects", _DEFAULT_MAX_OBJECTS))
    return {"cluster": cluster_ref(cluster_moid), "syncingObjectFilter": sync_filter}


def _sync_detail(details: Any, status: str) -> dict[str, Any]:
    details = details if isinstance(details, dict) else {}
    objects_key, bytes_key, seconds_key = _SYNC_DETAIL_KEYS[status]
    return {
        "status": status,
        "objects": as_int(details.get(objects_key)),
        "bytes_left": as_int(details.get(bytes_key)),
        "seconds_left": as_int(details.get(seconds_key)),
    }


def _component_rows(objects: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for obj in dict_items(objects):
        object_uuid = as_text(obj.get("uuid"))
        for component in dict_items(obj.get("components")):
            rows.append(
                {
                    "object_uuid": object_uuid,
                    "component_uuid": as_text(component.get("uuid")),
                    "host_uuid": as_text(component.get("hostUuid")),
                    "disk_uuid": as_text(component.get("diskUuid")),
                    "bytes_left": as_int(component.get("bytesToSync")),
                    "seconds_left": as_int(component.get("recoveryETA")),
                    "reasons": ", ".join(text_items(component.get("reasons"))) or None,
                }
            )
    return rows


async def vsan_resync_status_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.vsan.resync.status``."""
    cluster_moid = params["cluster"]
    result = await vsan_call(
        connector,
        operator,
        target,
        VSAN_QUERY_SYNCING_OBJECTS,
        build_syncing_filter_body(cluster_moid, params),
        what="resync status",
    )
    if not isinstance(result, dict):
        raise RuntimeError("vSAN resync status: vCenter sent no VsanObjectSyncQueryResult")
    details = result.get("syncingObjectRecoveryDetails")
    components = _component_rows(result.get("objects"))
    _log().info(
        "vmware_vsan_resync_status_read",
        target=target.name,
        cluster=cluster_moid,
        component_count=len(components),
    )
    return {
        "cluster": cluster_moid,
        "objects_left": as_int(result.get("totalObjectsToSync")),
        "bytes_left": as_int(result.get("totalBytesToSync")),
        "seconds_left": as_int(result.get("totalRecoveryETA")),
        "by_status": [_sync_detail(details, status) for status in _SYNC_DETAIL_KEYS],
        "components": components,
    }


# ---------------------------------------------------------------------------
# vmware.vsan.object.health
# ---------------------------------------------------------------------------


def health_group(state: str | None) -> str:
    """The plain group of one vSAN object health state."""
    if state == "healthy":
        return HEALTH_GROUP_HEALTHY
    if state == "inaccessible":
        return HEALTH_GROUP_INACCESSIBLE
    if state is not None and state.startswith("reducedavailability"):
        return HEALTH_GROUP_REDUCED
    return HEALTH_GROUP_OTHER


async def _identities(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    cluster_moid: str,
    uuids: list[str],
) -> dict[str, dict[str, Any]]:
    """``{uuid: VsanObjectIdentity}`` of the objects *uuids*."""
    answer = await vsan_call(
        connector,
        operator,
        target,
        VSAN_QUERY_OBJECT_IDENTITIES,
        {
            "cluster": cluster_ref(cluster_moid),
            "objUuids": uuids,
            "includeHealth": True,
            "includeObjIdentity": True,
        },
        what="object identities",
    )
    identities = answer.get("identities") if isinstance(answer, dict) else None
    return {
        uuid: identity
        for identity in dict_items(identities)
        if (uuid := as_text(identity.get("uuid")))
    }


def _object_row(uuid: str, state: str | None, identity: dict[str, Any]) -> dict[str, Any]:
    vm = identity.get("vm")
    return {
        "object_uuid": uuid,
        "health": state,
        "health_group": health_group(state),
        "object_type": as_text(identity.get("type")),
        "vm": as_text(vm.get("value")) if isinstance(vm, dict) else None,
        "vm_instance_uuid": as_text(identity.get("vmInstanceUuid")),
        "description": as_text(identity.get("description")),
        "storage_policy": as_text(identity.get("spbmProfileName")),
    }


async def vsan_object_health_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.vsan.object.health``."""
    cluster_moid = params["cluster"]
    from_cache = bool(params.get("from_cache", False))
    summary = await vsan_call(
        connector,
        operator,
        target,
        VSAN_QUERY_HEALTH_SUMMARY,
        health_summary_body(
            cluster_moid, field="objectHealth", include_obj_uuids=True, from_cache=from_cache
        ),
        what="object health",
    )
    object_health = summary.get("objectHealth") if isinstance(summary, dict) else None
    if not isinstance(object_health, dict):
        raise RuntimeError("vSAN object health: vCenter sent no objectHealth")
    counts: dict[str, int] = {}
    states: dict[str, str | None] = {}
    rows: list[dict[str, Any]] = []
    for detail in dict_items(object_health.get("objectHealthDetail")):
        state = as_text(detail.get("health"))
        number = as_int(detail.get("numObjects")) or 0
        rows.append({"health": state, "health_group": health_group(state), "objects": number})
        counts[health_group(state)] = counts.get(health_group(state), 0) + number
        if health_group(state) != HEALTH_GROUP_HEALTHY:
            states.update(dict.fromkeys(text_items(detail.get("objUuids")), state))
    identities = (
        await _identities(connector, operator, target, cluster_moid, list(states)) if states else {}
    )
    _log().info(
        "vmware_vsan_object_health_read",
        target=target.name,
        cluster=cluster_moid,
        unhealthy=len(states),
    )
    return {
        "cluster": cluster_moid,
        "summary": {
            group: counts.get(group, 0)
            for group in (
                HEALTH_GROUP_HEALTHY,
                HEALTH_GROUP_REDUCED,
                HEALTH_GROUP_INACCESSIBLE,
                HEALTH_GROUP_OTHER,
            )
        },
        "by_state": rows,
        "unhealthy_objects": [
            _object_row(uuid, state, identities.get(uuid, {})) for uuid, state in states.items()
        ],
        "checked_at": as_text(summary.get("timestamp")),
        "from_cache": from_cache,
    }


# ---------------------------------------------------------------------------
# Op metadata
# ---------------------------------------------------------------------------

VMWARE_VSAN_RESYNC_STATUS_OP: Final[VmwareTypedOp] = VmwareTypedOp(
    op_id="vmware.vsan.resync.status",
    handler_attr="vsan_resync_status",
    summary="vSAN objects that resync now in a cluster: bytes left, time left and reason.",
    description=(
        "Returns the vSAN resync of one cluster: objects, bytes and time left in total and "
        "for active, queued and suspended resyncs, and one row per syncing component with "
        "its bytes and time left and why it resyncs."
    ),
    parameter_schema=RESYNC_PARAMETER_SCHEMA,
    response_schema=RESYNC_RESPONSE_SCHEMA,
    group_key=VSAN_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "vsan", "resync"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=RESYNC_LLM_INSTRUCTIONS,
)

VMWARE_VSAN_OBJECT_HEALTH_OP: Final[VmwareTypedOp] = VmwareTypedOp(
    op_id="vmware.vsan.object.health",
    handler_attr="vsan_object_health",
    summary=(
        "vSAN object health of a cluster: healthy, reduced redundancy, inaccessible, and their VMs."
    ),
    description=(
        "Returns how many vSAN objects of one cluster are healthy, have reduced redundancy "
        "or are inaccessible, and one row per object that is not healthy with its type, "
        "its VM and its storage policy."
    ),
    parameter_schema={
        "type": "object",
        "properties": {"cluster": CLUSTER_PARAM_SCHEMA, "from_cache": FROM_CACHE_PARAM_SCHEMA},
        "required": ["cluster"],
        "additionalProperties": False,
        "examples": [{"cluster": "<cluster-id>"}],
    },
    response_schema=OBJECT_HEALTH_RESPONSE_SCHEMA,
    group_key=VSAN_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "vsan", "health", "objects"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=OBJECT_HEALTH_LLM_INSTRUCTIONS,
)
