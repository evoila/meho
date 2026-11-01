# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.vsan.capacity`` typed op -- capacity and space use of a vSAN cluster.

Two vSAN calls on the connector's own session (VI-JSON on vCenter 9.x, SOAP
on 8.0, see :meth:`VmwareRestConnector._post_vsan`):

1. ``VsanSpaceReportSystem.VsanQuerySpaceUsage`` (object
   ``vsan-cluster-space-report-system``): total and free capacity, the use by
   object type, the reserved space, and the savings of deduplication and
   compression (``VsanSpaceUsage``).
2. ``VsanVcClusterHealthSystem.VsanQueryVcClusterHealthSummary`` with
   ``fields: ["limitHealth"]``: only the limit part of the health summary,
   whose ``whatifHostFailures`` says what is left after 0, 1 or more host
   failures. ``includeObjUuids: false`` keeps the answer small.
   ``fetchFromCache`` follows the caller's ``from_cache``, as for
   ``vmware.host.vsan_health``.

Field names: the vendor VI/JSON file of vCenter 9.0 (``VsanSpaceUsage``,
``VsanObjectSpaceSummary``, ``VimVsanDataEfficiencyCapacityState``,
``VsanClusterLimitHealthResult``, ``VsanClusterWhatifHostFailuresResult``).
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
    VSAN_WHEN_TO_USE,
    as_float,
    as_int,
    as_text,
    cluster_ref,
    dict_items,
    health_summary_body,
    vsan_call,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike

__all__ = [
    "TYPED_OPS",
    "VMWARE_VSAN_CAPACITY_OP",
    "VSAN_QUERY_SPACE_USAGE",
    "WHEN_TO_USE_BY_GROUP",
    "vsan_capacity_impl",
]

VSAN_QUERY_SPACE_USAGE: Final = VsanMethod(
    mo_type="VsanSpaceReportSystem",
    moid="vsan-cluster-space-report-system",
    method="VsanQuerySpaceUsage",
)

#: ``VsanObjectSpaceSummary`` fields -> answer fields (all in bytes).
_SPACE_FIELDS: Final = (
    ("usedB", "used_bytes"),
    ("physicalUsedB", "physical_used_bytes"),
    ("primaryCapacityB", "primary_bytes"),
    ("overheadB", "overhead_bytes"),
    ("temporaryOverheadB", "temporary_overhead_bytes"),
    ("reservedCapacityB", "reserved_bytes"),
    ("overReservedB", "over_reserved_bytes"),
    ("provisionCapacityB", "provisioned_bytes"),
    ("snapshotUsedB", "snapshot_used_bytes"),
)


def _log() -> Any:
    """Get the logger per call, so ``structlog.testing.capture_logs`` sees every event."""
    return structlog.get_logger(__name__)


def _space(summary: dict[str, Any]) -> dict[str, Any]:
    return {name: as_int(summary.get(key)) for key, name in _SPACE_FIELDS}


def _by_object_type(usage: dict[str, Any]) -> list[dict[str, Any]]:
    detail = usage.get("spaceDetail")
    entries = dict_items(detail.get("spaceUsageByObjectType")) if isinstance(detail, dict) else []
    return [{"object_type": as_text(entry.get("objType")), **_space(entry)} for entry in entries]


def _efficiency(usage: dict[str, Any]) -> dict[str, Any] | None:
    """Deduplication and compression; ``None`` when space saving is off."""
    state = usage.get("efficientCapacity")
    ratio = usage.get("spaceEfficiencyRatio")
    if not isinstance(state, dict) and not isinstance(ratio, dict):
        return None
    state = state if isinstance(state, dict) else {}
    ratio = ratio if isinstance(ratio, dict) else {}
    logical_used = as_int(state.get("logicalCapacityUsed"))
    physical_used = as_int(state.get("physicalCapacityUsed"))
    esa_dedup = as_int(state.get("esaDedupSpaceSaving"))
    esa_compression = as_int(state.get("esaCompressionSpaceSaving"))
    if esa_dedup is not None or esa_compression is not None:
        saved = (esa_dedup or 0) + (esa_compression or 0)
    elif logical_used is not None and physical_used is not None:
        saved = logical_used - physical_used
    else:
        saved = None
    return {
        "saved_bytes": saved,
        "logical_used_bytes": logical_used,
        "physical_used_bytes": physical_used,
        "dedup_metadata_bytes": as_int(state.get("dedupMetadataSize")),
        "overall_ratio": as_float(ratio.get("overallRatio")),
        "dedup_ratio": as_float(ratio.get("dedupRatio")),
        "compression_ratio": as_float(ratio.get("compressionRatio")),
    }


def _threshold(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "yellow_bytes": as_int(value.get("yellowValue")),
        "red_bytes": as_int(value.get("redValue")),
    }


def _host_failure_row(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "host_failures": as_int(result.get("numFailures")),
        "total_bytes": as_int(result.get("totalCapacityB")),
        "used_bytes": as_int(result.get("totalUsedCapacityB")),
        "used_components": as_int(result.get("usedComponents")),
        "total_components": as_int(result.get("totalComponents")),
        "disk_space_health": as_text(result.get("diskFreeSpaceHealth")),
        "component_limit_health": as_text(result.get("componentLimitHealth")),
        "space_needed_for_operations_bytes": as_int(result.get("slackSpaceCapRequired")),
    }


async def vsan_capacity_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.vsan.capacity``."""
    cluster_moid = params["cluster"]
    from_cache = bool(params.get("from_cache", False))
    usage = await vsan_call(
        connector,
        operator,
        target,
        VSAN_QUERY_SPACE_USAGE,
        {"cluster": cluster_ref(cluster_moid)},
        what="space usage",
    )
    if not isinstance(usage, dict) or as_int(usage.get("totalCapacityB")) is None:
        raise RuntimeError("vSAN space usage: vCenter sent no total capacity")
    summary = await vsan_call(
        connector,
        operator,
        target,
        VSAN_QUERY_HEALTH_SUMMARY,
        health_summary_body(
            cluster_moid, field="limitHealth", include_obj_uuids=False, from_cache=from_cache
        ),
        what="capacity limits",
    )
    limit = summary.get("limitHealth") if isinstance(summary, dict) else None
    if not isinstance(limit, dict):
        raise RuntimeError("vSAN capacity limits: vCenter sent no limitHealth")
    total = as_int(usage.get("totalCapacityB"))
    free = as_int(usage.get("freeCapacityB"))
    overview = usage.get("spaceOverview")
    answer = {
        "cluster": cluster_moid,
        "total_bytes": total,
        "free_bytes": free,
        "used_bytes": total - free if total is not None and free is not None else None,
        "uncommitted_bytes": as_int(usage.get("uncommittedB")),
        "overview": _space(overview) if isinstance(overview, dict) else None,
        "by_object_type": _by_object_type(usage),
        "space_saving": _efficiency(usage),
        "capacity_threshold": _threshold(usage.get("capacityHealthThreshold")),
        "host_failures": [
            _host_failure_row(r) for r in dict_items(limit.get("whatifHostFailures"))
        ],
        "checked_at": as_text(summary.get("timestamp")),
        "from_cache": from_cache,
    }
    _log().info(
        "vmware_vsan_capacity_read", target=target.name, cluster=cluster_moid, from_cache=from_cache
    )
    return answer


# ---------------------------------------------------------------------------
# Op metadata + schemas
# ---------------------------------------------------------------------------

_BYTES = "integer"


def _num(description: str, kind: str = _BYTES) -> dict[str, Any]:
    return {"type": [kind, "null"], "description": description}


_SPACE_PROPERTIES: dict[str, Any] = {
    "used_bytes": _num("Used space in bytes (the larger of physically used and reserved)."),
    "physical_used_bytes": _num("Space physically written, in bytes."),
    "primary_bytes": _num("User data without copies, in bytes."),
    "overhead_bytes": _num("Copies and parity for the storage policy, in bytes."),
    "temporary_overhead_bytes": _num("Space used for a while by resync and moves, in bytes."),
    "reserved_bytes": _num("Space reserved by thick provisioning, in bytes."),
    "over_reserved_bytes": _num("Reserved space that is not written yet, in bytes."),
    "provisioned_bytes": _num("Size the users asked for, in bytes."),
    "snapshot_used_bytes": _num("Space of snapshots that can be freed, in bytes."),
}

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cluster": {"type": "string", "description": "Managed object id of the cluster."},
        "total_bytes": _num("Total vSAN capacity in bytes."),
        "free_bytes": _num("Free vSAN capacity in bytes."),
        "used_bytes": _num("total_bytes minus free_bytes."),
        "uncommitted_bytes": _num("Space thin objects may still use, in bytes."),
        "overview": {
            "type": ["object", "null"],
            "description": "Space use of the whole cluster.",
            "properties": _SPACE_PROPERTIES,
        },
        "by_object_type": {
            "type": "array",
            "description": "Space use per vSAN object type (vdisk, vmswap, namespace, ...).",
            "items": {
                "type": "object",
                "properties": {
                    "object_type": _num("The vSAN object type.", "string"),
                    **_SPACE_PROPERTIES,
                },
            },
        },
        "space_saving": {
            "type": ["object", "null"],
            "description": "Deduplication and compression. null when space saving is off.",
            "properties": {
                "saved_bytes": _num("Space saved by deduplication and compression, in bytes."),
                "logical_used_bytes": _num("Used space before saving, in bytes."),
                "physical_used_bytes": _num("Used space after saving, in bytes."),
                "dedup_metadata_bytes": _num("Space for deduplication metadata, in bytes."),
                "overall_ratio": _num("Saving ratio, for example 1.8.", "number"),
                "dedup_ratio": _num("Ratio from deduplication.", "number"),
                "compression_ratio": _num("Ratio from compression.", "number"),
            },
        },
        "capacity_threshold": {
            "type": ["object", "null"],
            "description": "Used space at which vSAN health turns yellow or red.",
            "properties": {
                "yellow_bytes": _num("Yellow limit in bytes."),
                "red_bytes": _num("Red limit in bytes."),
            },
        },
        "host_failures": {
            "type": "array",
            "description": "What is left after 0, 1 or more host failures.",
            "items": {
                "type": "object",
                "properties": {
                    "host_failures": _num("Number of failed hosts in this row."),
                    "total_bytes": _num("Capacity left in bytes."),
                    "used_bytes": _num("Used space after the failures, in bytes."),
                    "used_components": _num("vSAN components in use."),
                    "total_components": _num("Most vSAN components the hosts can hold."),
                    "disk_space_health": _num("green, yellow or red.", "string"),
                    "component_limit_health": _num("green, yellow or red.", "string"),
                    "space_needed_for_operations_bytes": _num(
                        "Space vSAN needs for its own work, such as resync, in bytes."
                    ),
                },
            },
        },
        "checked_at": _num("Time of the health check the host-failure rows come from.", "string"),
        "from_cache": {"type": "boolean", "description": "True when from_cache was asked."},
    },
    "required": ["cluster", "total_bytes", "free_bytes", "by_object_type", "host_failures"],
}

VMWARE_VSAN_CAPACITY_OP: Final[VmwareTypedOp] = VmwareTypedOp(
    op_id="vmware.vsan.capacity",
    handler_attr="vsan_capacity",
    summary=(
        "vSAN capacity of a cluster: total, used, free, use by object type, savings, host failures."
    ),
    description=(
        "Returns the vSAN capacity of one cluster: total, used and free bytes, the use by "
        "object type, reserved space, the savings of deduplication and compression, and the "
        "space left after one or more host failures."
    ),
    parameter_schema={
        "type": "object",
        "properties": {"cluster": CLUSTER_PARAM_SCHEMA, "from_cache": FROM_CACHE_PARAM_SCHEMA},
        "required": ["cluster"],
        "additionalProperties": False,
        "examples": [{"cluster": "<cluster-id>"}],
    },
    response_schema=_RESPONSE_SCHEMA,
    group_key=VSAN_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "vsan", "capacity", "storage"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call when the question is how full vSAN is, what uses the space, how much "
            "deduplication and compression save, or whether the cluster can lose a host."
        ),
        "parameter_hints": {
            "cluster": "Cluster moid from vmware.cluster.list.",
            "from_cache": "true while a host is down or in maintenance mode. Default false.",
        },
        "output_shape": (
            "{cluster, total_bytes, free_bytes, used_bytes, uncommitted_bytes, overview: "
            "{used_bytes, physical_used_bytes, primary_bytes, overhead_bytes, reserved_bytes, "
            "...}, by_object_type: [{object_type, used_bytes, ...}], space_saving: {saved_bytes, "
            "overall_ratio, ...} or null, capacity_threshold: {yellow_bytes, red_bytes}, "
            "host_failures: [{host_failures, total_bytes, used_bytes, disk_space_health, ...}], "
            "checked_at, from_cache}. All sizes are bytes. A failed vSAN call returns an error "
            "with vCenter's fault."
        ),
        "next_step": (
            "Compare used_bytes with capacity_threshold. The host_failures row with "
            "host_failures 1 says if the data still fits after one host fails. Before a "
            "host goes into maintenance mode, call vmware.vsan.maintenance.precheck."
        ),
    },
)

TYPED_OPS: tuple[VmwareTypedOp, ...] = (VMWARE_VSAN_CAPACITY_OP,)
WHEN_TO_USE_BY_GROUP: dict[str, str] = {VSAN_GROUP_KEY: VSAN_WHEN_TO_USE}
