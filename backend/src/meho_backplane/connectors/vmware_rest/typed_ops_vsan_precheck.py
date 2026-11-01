# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.vsan.maintenance.precheck`` -- what a host's maintenance mode would do to vSAN data.

vSAN's resource check simulates the maintenance mode of one host with one
vSAN data option and reports what would happen: how much data moves, which
objects would lose redundancy or become inaccessible, and whether the other
hosts have room. It changes nothing; it runs as a vCenter task.

Calls, on the connector's own session:

1. ``HostSystem.parent`` through ``PropertyCollector.RetrievePropertiesEx``:
   the host's cluster.
2. The cluster's ``configurationEx`` (its ``vsanHostConfig``): the host's vSAN
   node UUID, which the check names the host by.
3. vSAN ``VsanResourceCheckSystem.VsanPerformResourceCheck`` (object
   ``vsan-cluster-resource-check-system``) with a ``VsanResourceCheckSpec``
   for ``EnterMaintenanceMode``. It returns a vCenter task.
4. The task is polled to its end (:func:`.vim_task.poll_vim_task`).
5. vSAN ``VsanResourceCheckSystem.VsanGetResourceCheckStatus`` with the same
   spec: the result.

vSAN calls use VI-JSON on vCenter 9.x and SOAP on 8.0
(:meth:`VmwareRestConnector._post_vsan`). Types and fields: the vendor
VI/JSON file of vCenter 9.0 (``VsanResourceCheckSpec``,
``VsanResourceCheckStatus``, ``VsanResourceCheckResult``,
``HostMaintenanceSpec``, ``VsanHostDecommissionModeObjectAction_enum``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import structlog

from meho_backplane.connectors.vmware_rest._mount import VsanMethod
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp
from meho_backplane.connectors.vmware_rest.typed_ops_vsan_cluster_info import vsan_hosts
from meho_backplane.connectors.vmware_rest.typed_ops_vsan_common import (
    CLUSTER_MO_TYPE,
    HOST_MO_TYPE,
    VSAN_GROUP_KEY,
    as_bool,
    as_int,
    as_text,
    cluster_ref,
    dict_items,
    read_properties,
    text_items,
    vsan_call,
)
from meho_backplane.connectors.vmware_rest.typed_ops_vsan_precheck_schemas import (
    PRECHECK_LLM_INSTRUCTIONS,
    PRECHECK_PARAMETER_SCHEMA,
    PRECHECK_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.vim_task import poll_vim_task

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike

__all__ = [
    "CHECK_TIMEOUT_SECONDS",
    "VMWARE_VSAN_MAINTENANCE_PRECHECK_OP",
    "VSAN_GET_RESOURCE_CHECK_STATUS",
    "VSAN_PERFORM_RESOURCE_CHECK",
    "build_resource_check_body",
    "vsan_maintenance_precheck_impl",
]

_RESOURCE_CHECK_SYSTEM: Final = "VsanResourceCheckSystem"
_RESOURCE_CHECK_MOID: Final = "vsan-cluster-resource-check-system"

VSAN_PERFORM_RESOURCE_CHECK: Final = VsanMethod(
    mo_type=_RESOURCE_CHECK_SYSTEM, moid=_RESOURCE_CHECK_MOID, method="VsanPerformResourceCheck"
)
VSAN_GET_RESOURCE_CHECK_STATUS: Final = VsanMethod(
    mo_type=_RESOURCE_CHECK_SYSTEM, moid=_RESOURCE_CHECK_MOID, method="VsanGetResourceCheckStatus"
)

_OPERATION_ENTER_MAINTENANCE: Final = "EnterMaintenanceMode"
_DEFAULT_VSAN_MODE: Final = "ensureObjectAccessibility"
_STATUS_COMPLETED: Final = "resourceCheckCompleted"
_PROP_PARENT: Final = "parent"

#: How long the op waits for the check task. Tests set the poll interval to 0.
CHECK_TIMEOUT_SECONDS: Final = 300.0
POLL_INTERVAL_SECONDS = 2.0


def _log() -> Any:
    """Get the logger per call, so ``structlog.testing.capture_logs`` sees every event."""
    return structlog.get_logger(__name__)


def build_resource_check_body(cluster_moid: str, node_uuid: str, vsan_mode: str) -> dict[str, Any]:
    """The body of both resource-check methods: the spec, then the cluster.

    Keys follow the order the vendor types define; SOAP needs it.
    """
    return {
        "resourceCheckSpec": {
            "_typeName": "VsanResourceCheckSpec",
            "operation": _OPERATION_ENTER_MAINTENANCE,
            "entities": [node_uuid],
            "maintenanceSpec": {
                "_typeName": "HostMaintenanceSpec",
                "vsanMode": {"_typeName": "VsanHostDecommissionMode", "objectAction": vsan_mode},
            },
        },
        "cluster": cluster_ref(cluster_moid),
    }


async def _cluster_and_node(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    host_moid: str,
) -> tuple[str, str]:
    """``(cluster moid, vSAN node UUID)`` of the host; raises when either is missing."""
    props = await read_properties(
        connector, operator, target, HOST_MO_TYPE, [host_moid], (_PROP_PARENT,)
    )
    parent = props.get(host_moid, {}).get(_PROP_PARENT)
    if not isinstance(parent, dict) or parent.get("type") != CLUSTER_MO_TYPE:
        raise RuntimeError(f"vSAN maintenance pre-check: host {host_moid!r} is not in a cluster")
    cluster_moid = as_text(parent.get("value")) or ""
    hosts = await vsan_hosts(connector, operator, target, cluster_moid, with_names=False)
    node_uuid = next((row["node_uuid"] for row in hosts if row["host"] == host_moid), None)
    if not node_uuid:
        raise RuntimeError(
            f"vSAN maintenance pre-check: host {host_moid!r} has no vSAN node UUID; "
            "vSAN may be off for this host"
        )
    return cluster_moid, node_uuid


def _entity_row(level: str, entity: dict[str, Any]) -> dict[str, Any]:
    host = entity.get("host")
    return {
        "level": level,
        "name": as_text(entity.get("name")),
        "host": as_text(host.get("value")) if isinstance(host, dict) else None,
        "is_new": as_bool(entity.get("isNew")),
        "capacity_bytes": as_int(entity.get("capacity")),
        "used_bytes": as_int(entity.get("usedCapacity")),
        "capacity_after_bytes": as_int(entity.get("postOperationCapacity")),
        "used_after_bytes": as_int(entity.get("postOperationUsedCapacity")),
        "extra_capacity_needed_bytes": as_int(entity.get("additionalRequiredCapacity")),
        "components_after": as_int(entity.get("components")),
        "max_components": as_int(entity.get("maxComponents")),
    }


def _entity_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for fault_domain in dict_items(result.get("faultDomains")):
        rows.append(_entity_row("fault_domain", fault_domain))
        rows.extend(_entity_row("host", host) for host in dict_items(fault_domain.get("hosts")))
    return rows


def _messages(result: dict[str, Any]) -> list[str]:
    texts = []
    for message in dict_items(result.get("messages")):
        text = as_text(message.get("message")) or as_text(message.get("key"))
        if text:
            texts.append(text)
    return texts


def _result_fields(result: Any) -> dict[str, Any]:
    result = result if isinstance(result, dict) else {}
    inaccessible = text_items(result.get("inaccessibleObjects"))
    non_compliant = text_items(result.get("nonCompliantObjects"))
    return {
        "result": as_text(result.get("status")),
        "checked_at": as_text(result.get("timestamp")),
        "data_to_move_bytes": as_int(result.get("dataToMove")),
        "data_to_resync_bytes": as_int(result.get("dataToResync")),
        "inaccessible_objects": len(inaccessible),
        "inaccessible_object_uuids": inaccessible,
        "non_compliant_objects": len(non_compliant),
        "non_compliant_object_uuids": non_compliant,
        "messages": _messages(result),
        "entities": _entity_rows(result),
    }


async def vsan_maintenance_precheck_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.vsan.maintenance.precheck``."""
    host_moid = params["host"]
    vsan_mode = params.get("vsan_mode") or _DEFAULT_VSAN_MODE
    cluster_moid, node_uuid = await _cluster_and_node(connector, operator, target, host_moid)
    body = build_resource_check_body(cluster_moid, node_uuid, vsan_mode)
    task = await vsan_call(
        connector, operator, target, VSAN_PERFORM_RESOURCE_CHECK, body, what="resource check"
    )
    outcome = await poll_vim_task(
        connector,
        target,
        operator,
        task=task,
        timeout_seconds=CHECK_TIMEOUT_SECONDS,
        poll_interval=POLL_INTERVAL_SECONDS,
    )
    if not outcome.succeeded and not outcome.timed_out:
        reason = outcome.error_message or outcome.fault_type or "the vCenter task failed"
        raise RuntimeError(f"vSAN maintenance pre-check of host {host_moid!r} failed: {reason}")
    status = await vsan_call(
        connector, operator, target, VSAN_GET_RESOURCE_CHECK_STATUS, body, what="resource check"
    )
    if not isinstance(status, dict):
        raise RuntimeError("vSAN maintenance pre-check: vCenter sent no VsanResourceCheckStatus")
    check_status = as_text(status.get("status"))
    if not outcome.timed_out and check_status != _STATUS_COMPLETED:
        raise RuntimeError(
            f"vSAN maintenance pre-check of host {host_moid!r} did not complete: "
            f"vCenter reports status {check_status!r}"
        )
    if check_status == _STATUS_COMPLETED:
        result = status.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("vSAN maintenance pre-check: the check ended but sent no result")
        if as_text(result.get("status")) is None:
            raise RuntimeError("vSAN maintenance pre-check: the result has no status")
    note = None
    if outcome.timed_out:
        note = (
            f"The check did not end within {CHECK_TIMEOUT_SECONDS:g} seconds and runs on in "
            f"vCenter (task {outcome.task}). Call again later for the result."
        )
    _log().info(
        "vmware_vsan_maintenance_precheck_read",
        target=target.name,
        host=host_moid,
        cluster=cluster_moid,
        vsan_mode=vsan_mode,
        check_status=check_status,
    )
    return {
        "host": host_moid,
        "cluster": cluster_moid,
        "vsan_mode": vsan_mode,
        "check_status": check_status,
        **_result_fields(status.get("result")),
        "task": outcome.task,
        "note": note,
    }


VMWARE_VSAN_MAINTENANCE_PRECHECK_OP: Final[VmwareTypedOp] = VmwareTypedOp(
    op_id="vmware.vsan.maintenance.precheck",
    handler_attr="vsan_maintenance_precheck",
    summary="What a host's maintenance mode would do to vSAN data, for one vSAN data option.",
    description=(
        "Runs vSAN's resource check for one host and one vSAN data option, without changing "
        "anything. Returns green, yellow or red, the data that would move, the objects that "
        "would become inaccessible or lose redundancy, and the space on each host after."
    ),
    parameter_schema=PRECHECK_PARAMETER_SCHEMA,
    response_schema=PRECHECK_RESPONSE_SCHEMA,
    group_key=VSAN_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "vsan", "maintenance", "precheck"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=PRECHECK_LLM_INSTRUCTIONS,
)
