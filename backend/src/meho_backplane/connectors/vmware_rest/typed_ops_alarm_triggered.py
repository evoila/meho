# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.alarm.triggered.list`` typed op (#4383, part 1).

Lists the alarms that are triggered now (red or yellow) in a vCenter, or under
one object (a cluster, host, VM, folder ...). One flat row per triggered
alarm: the alarm id and name, its trigger kind, the object it is triggered on,
the status, the time, and whether and by whom it was acknowledged.

It is a ``source_kind="typed"`` bound method on
:class:`VmwareRestConnector`, like ``vmware.tasks.recent``: it reads directly
on the connector session, so it works on a fresh install with no spec ingest.
The reads are in :mod:`~meho_backplane.connectors.vmware_rest.alarm_state`,
shared with the alarm acknowledge and reset composites. A long list is stored
behind a result handle by the dispatcher, like every other set-shaped result.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vmware_rest.alarm_state import (
    ALARM_ENTITY_TYPES,
    ALARMS_GROUP_KEY,
    ALARMS_WHEN_TO_USE,
    read_service_content_refs,
    read_triggered_alarms,
)
from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "ALARM_TRIGGERED_GROUP_KEY",
    "ALARM_TRIGGERED_WHEN_TO_USE",
    "VMWARE_ALARM_TRIGGERED_LIST_OP",
    "alarm_triggered_list_impl",
]

_log = structlog.get_logger(__name__)

ALARM_TRIGGERED_GROUP_KEY = ALARMS_GROUP_KEY
ALARM_TRIGGERED_WHEN_TO_USE = ALARMS_WHEN_TO_USE

_ROOT_FOLDER_MO_TYPE = "Folder"


async def alarm_triggered_list_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.alarm.triggered.list``.

    Without ``entity_id`` it reads the root folder from the ServiceContent and
    lists the triggered alarms of the whole vCenter. With ``entity_type`` and
    ``entity_id`` it lists the triggered alarms of that object and the objects
    below it; a missing object gives MEHO's normal ``not_found`` answer.

    Returns ``{"scope": {entity_type, entity_id}, "alarms": [...],
    "read_note"}``.
    """
    entity_id = params.get("entity_id")
    if isinstance(entity_id, str) and entity_id:
        mo_type, moid, promote = str(params["entity_type"]), entity_id, True
    else:
        root_folder, _ = await read_service_content_refs(connector, target, operator)
        mo_type, moid, promote = _ROOT_FOLDER_MO_TYPE, root_folder, False
    rows, note = await read_triggered_alarms(
        connector, target, operator, mo_type=mo_type, moid=moid, promote_missing=promote
    )
    _log.info(
        "vmware_alarm_triggered_list_read",
        target=target.name,
        mo_type=mo_type,
        alarm_count=len(rows),
        names_complete=note is None,
    )
    return {
        "scope": {"entity_type": mo_type, "entity_id": moid},
        "alarms": rows,
        "read_note": note,
    }


# ---------------------------------------------------------------------------
# Op metadata + schemas
# ---------------------------------------------------------------------------

_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entity_type": {
            "type": "string",
            "enum": list(ALARM_ENTITY_TYPES),
            "description": (
                "Type of the object to list the alarms under, for example HostSystem, "
                "ClusterComputeResource, VirtualMachine or Folder. Give it together with "
                "entity_id. Leave both out to list the whole vCenter."
            ),
        },
        "entity_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "description": (
                "Managed object id of that object, as vSphere reads return it. Give it "
                "together with entity_type. The list holds the alarms of this object and "
                "of the objects below it."
            ),
        },
    },
    "dependentRequired": {"entity_type": ["entity_id"], "entity_id": ["entity_type"]},
    "additionalProperties": False,
    "examples": [{}, {"entity_type": "HostSystem", "entity_id": "<host-id>"}],
}

_ALARM_ROW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "alarm_id": {"type": "string", "description": "Managed object id of the alarm."},
        "alarm_name": {
            "type": ["string", "null"],
            "description": "Name of the alarm; null when the name read failed.",
        },
        "trigger_kind": {
            "type": ["string", "null"],
            "enum": ["event", "metric", "mixed", None],
            "description": (
                "What fires the alarm: event (an event), metric (a metric or a state), "
                "mixed (both), null (not known)."
            ),
        },
        "entity_id": {
            "type": "string",
            "description": "Managed object id of the object the alarm is triggered on.",
        },
        "entity_type": {
            "type": "string",
            "description": "Type of that object, for example HostSystem or Folder.",
        },
        "entity_name": {
            "type": ["string", "null"],
            "description": "Name of that object; null when the name read failed.",
        },
        "status": {
            "type": ["string", "null"],
            "enum": ["red", "yellow", "green", "gray", None],
            "description": "Status of the alarm: red or yellow while it is triggered.",
        },
        "time": {
            "type": ["string", "null"],
            "description": "When the alarm was triggered (ISO 8601).",
        },
        "acknowledged": {
            "type": "boolean",
            "description": "True when someone acknowledged the alarm.",
        },
        "acknowledged_by": {
            "type": ["string", "null"],
            "description": "User who acknowledged it; null when not acknowledged.",
        },
        "acknowledged_time": {
            "type": ["string", "null"],
            "description": "When it was acknowledged; null when not acknowledged.",
        },
    },
    "required": ["alarm_id", "entity_id", "entity_type", "acknowledged"],
}

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "scope": {
            "type": "object",
            "description": "The object that was read: the root folder for the whole vCenter.",
            "properties": {
                "entity_type": {"type": "string", "description": "Type of the object."},
                "entity_id": {"type": "string", "description": "Managed object id."},
            },
            "required": ["entity_type", "entity_id"],
        },
        "alarms": {
            "type": "array",
            "description": "One row per triggered alarm, red first, newest first.",
            "items": _ALARM_ROW_SCHEMA,
        },
        "read_note": {
            "type": ["string", "null"],
            "description": "Which name reads failed; null when all names were read.",
        },
    },
    "required": ["scope", "alarms"],
}

VMWARE_ALARM_TRIGGERED_LIST_OP = VmwareTypedOp(
    op_id="vmware.alarm.triggered.list",
    handler_attr="alarm_triggered_list",
    summary="List the vCenter alarms that are triggered now (red or yellow).",
    description=(
        "Returns one row per triggered alarm: alarm id and name, trigger kind, the object "
        "(id, type, name), status, time, and whether and by whom it was acknowledged. "
        "Reads the whole vCenter, or one object and the objects below it. Read-only."
    ),
    parameter_schema=_PARAMETER_SCHEMA,
    response_schema=_RESPONSE_SCHEMA,
    group_key=ALARM_TRIGGERED_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "alarms"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see which vCenter alarms are red or yellow now, for the whole vCenter "
            "or under one object (a cluster, host, VM or folder). Call it before you "
            "acknowledge or reset an alarm: those changes take the alarm_id and entity_id "
            "of a row."
        ),
        "parameter_hints": {
            "entity_type": "One of the enum types. Only together with entity_id.",
            "entity_id": "Id from an earlier read. Leave out both for the whole vCenter.",
        },
        "output_shape": (
            "{scope: {entity_type, entity_id}, alarms: [{alarm_id, alarm_name, "
            "trigger_kind: event|metric|mixed|null, entity_id, entity_type, entity_name, "
            "status: red|yellow, time, acknowledged, acknowledged_by, acknowledged_time}], "
            "read_note}. A long list comes back as a result handle; read it with "
            "result_query. alarm_name, trigger_kind and entity_name are null when their "
            "read failed; read_note says which read."
        ),
        "next_step": (
            "To stop the actions of an alarm while you work on its cause, call "
            "vmware.composite.alarm.acknowledge with its alarm_id and entity_id. After the "
            "cause is fixed, call vmware.composite.alarm.reset with the status, the object "
            "kind and the trigger_kind of the row. For the events behind an alarm, use "
            "vmware.composite.event.tail."
        ),
    },
)

TYPED_OPS: tuple[VmwareTypedOp, ...] = (VMWARE_ALARM_TRIGGERED_LIST_OP,)
WHEN_TO_USE_BY_GROUP: dict[str, str] = {ALARM_TRIGGERED_GROUP_KEY: ALARM_TRIGGERED_WHEN_TO_USE}
