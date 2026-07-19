# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.tasks.recent`` typed op (#2300).

Phase-1 "incident survival" read: the recent Task objects vCenter keeps
on the ``TaskManager`` -- change-window monitoring the shipped
``vmware.composite.event.tail`` (which reads the *event* log) does not
cover. Each row carries the operation, target entity, state, progress,
and the queued / started / completed timestamps an operator reads to
answer "what changed here, and did it finish?".

It is a ``source_kind="typed"`` bound method on
:class:`VmwareRestConnector` in the
:mod:`~meho_backplane.connectors.vmware_rest.typed_ops` mould: it reads
directly on the connector session (no ``dispatch_child``, no ingested
descriptor), so it works on a fresh boot with **zero catalog ingest**.

Two PropertyCollector reads (no history collector, no traversal):

1. ``TaskManager.recentTask`` -- the list of recent Task MoRefs vCenter
   maintains (bounded by vCenter, typically the last ~200).
2. ``Task.info`` on those MoRefs (a single ``RetrievePropertiesEx`` with
   one ``objectSet`` entry per task) -- the :class:`TaskInfo` per task.

Field names are the vim25 / VI-JSON wire contract (camelCase); the
``state`` enum (``queued`` / ``running`` / ``success`` / ``error``) is
passed through verbatim.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp, _unwrap_value

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "TASKS_RECENT_GROUP_KEY",
    "TASKS_RECENT_WHEN_TO_USE",
    "VMWARE_TASKS_RECENT_OP",
    "build_recent_tasks_retrieve_params",
    "build_task_info_retrieve_params",
    "tasks_recent_impl",
]

_log = structlog.get_logger(__name__)

_RETRIEVE_PROPERTIES_PATH = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"
_TASK_MANAGER_MO_TYPE = "TaskManager"
# The TaskManager is a singleton whose moId is 'TaskManager'.
_TASK_MANAGER_MOID = "TaskManager"
_TASK_MO_TYPE = "Task"
_PROP_RECENT_TASK = "recentTask"
_PROP_INFO = "info"

#: Default / maximum number of tasks surfaced. ``recentTask`` is already
#: bounded by vCenter; ``max_tasks`` caps how many of those the info read
#: fetches. The maximum is enforced declaratively via
#: ``parameter_schema.maximum``.
_DEFAULT_MAX_TASKS = 50
_MAX_TASKS_CAP = 200

TASKS_RECENT_GROUP_KEY = "vmware-tasks-recent"


def build_recent_tasks_retrieve_params() -> dict[str, Any]:
    """Build the ``RetrievePropertiesEx`` body reading ``TaskManager.recentTask``."""
    return {
        "specSet": [
            {
                "propSet": [{"type": _TASK_MANAGER_MO_TYPE, "pathSet": [_PROP_RECENT_TASK]}],
                "objectSet": [
                    {"obj": {"type": _TASK_MANAGER_MO_TYPE, "value": _TASK_MANAGER_MOID}}
                ],
            }
        ],
        "options": {},
    }


def build_task_info_retrieve_params(task_moids: list[str]) -> dict[str, Any]:
    """Build the ``RetrievePropertiesEx`` body reading ``info`` on each Task.

    One ``PropertyFilterSpec`` with a ``propSet`` for the ``Task.info``
    property and one ``objectSet`` entry per task moid -- a single round
    trip for every task, no traversal.
    """
    return {
        "specSet": [
            {
                "propSet": [{"type": _TASK_MO_TYPE, "pathSet": [_PROP_INFO]}],
                "objectSet": [
                    {"obj": {"type": _TASK_MO_TYPE, "value": moid}} for moid in task_moids
                ],
            }
        ],
        "options": {},
    }


def _moref_value(ref: Any) -> str | None:
    """Return the ``value`` moid from a VI-JSON MoRef, else ``None``.

    A MoRef serialises as ``{"type": ..., "value": "task-12"}``; a bare
    moid string is tolerated.
    """
    if isinstance(ref, dict):
        value = ref.get("value")
        return value if isinstance(value, str) else None
    return ref if isinstance(ref, str) else None


def _extract_recent_task_moids(retrieve_result: Any) -> list[str]:
    """Pull the ``recentTask`` MoRef list off the TaskManager result."""
    payload = _unwrap_value(retrieve_result)
    objects = payload.get("objects", []) if isinstance(payload, dict) else payload
    if not isinstance(objects, list):
        return []
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        for prop in obj.get("propSet", []) or []:
            if isinstance(prop, dict) and prop.get("name") == _PROP_RECENT_TASK:
                raw = prop.get("val")
                if isinstance(raw, list):
                    return [m for m in (_moref_value(r) for r in raw) if m is not None]
    return []


def _parse_task_info(task_moid: str, info: Any) -> dict[str, Any]:
    """Flatten one :class:`TaskInfo` into the operator-facing row."""
    ti = info if isinstance(info, dict) else {}
    error = ti.get("error")
    error_message = None
    if isinstance(error, dict):
        localized = error.get("localizedMessage")
        error_message = localized if isinstance(localized, str) else None
    entity = ti.get("entity")
    return {
        "task": task_moid,
        "operation": ti.get("descriptionId"),
        "entity": _moref_value(entity),
        "entity_type": entity.get("type") if isinstance(entity, dict) else None,
        "entity_name": ti.get("entityName"),
        "state": ti.get("state"),
        "progress": ti.get("progress"),
        "cancelled": ti.get("cancelled") if isinstance(ti.get("cancelled"), bool) else None,
        "queue_time": ti.get("queueTime"),
        "start_time": ti.get("startTime"),
        "complete_time": ti.get("completeTime"),
        "error_message": error_message,
    }


def _parse_task_info_results(retrieve_result: Any) -> dict[str, Any]:
    """Map each Task moid to its raw ``info`` value from the info read."""
    payload = _unwrap_value(retrieve_result)
    objects = payload.get("objects", []) if isinstance(payload, dict) else payload
    info_by_moid: dict[str, Any] = {}
    if not isinstance(objects, list):
        return info_by_moid
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        moid = _moref_value(obj.get("obj"))
        if moid is None:
            continue
        for prop in obj.get("propSet", []) or []:
            if isinstance(prop, dict) and prop.get("name") == _PROP_INFO:
                info_by_moid[moid] = prop.get("val")
    return info_by_moid


async def tasks_recent_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.tasks.recent`` -- recent Task objects.

    Reads, directly on the connector session (no ``dispatch_child``, no
    ingested descriptor):

    1. ``POST .../RetrievePropertiesEx`` reading
       ``TaskManager.recentTask`` -- the recent Task MoRefs.
    2. ``POST .../RetrievePropertiesEx`` reading ``Task.info``
       on those MoRefs (capped at ``max_tasks``) -- a single round trip.

    Both vmomi reads route through
    :meth:`VmwareRestConnector._post_vmomi_json`, which mounts them on the
    documented VI-JSON base ``/sdk/vim25/{release}`` (single ``/api``
    fallback) so they resolve on vCenter 8.0.x instead of 404ing (#2466).
    Returns ``{"tasks": [{task, operation, entity, entity_type,
    entity_name, state, progress, cancelled, queue_time, start_time,
    complete_time, error_message}, ...]}``, most recent as vCenter orders
    them.
    """
    max_tasks = params.get("max_tasks")
    if not isinstance(max_tasks, int) or isinstance(max_tasks, bool) or max_tasks < 1:
        max_tasks = _DEFAULT_MAX_TASKS

    recent_result = await connector._post_vmomi_json(
        target,
        _RETRIEVE_PROPERTIES_PATH,
        operator=operator,
        json=build_recent_tasks_retrieve_params(),
    )
    task_moids = _extract_recent_task_moids(recent_result)[:max_tasks]
    if not task_moids:
        _log.info("vmware_tasks_recent_read", target=target.name, task_count=0)
        return {"tasks": []}

    info_result = await connector._post_vmomi_json(
        target,
        _RETRIEVE_PROPERTIES_PATH,
        operator=operator,
        json=build_task_info_retrieve_params(task_moids),
    )
    info_by_moid = _parse_task_info_results(info_result)
    tasks = [_parse_task_info(moid, info_by_moid.get(moid)) for moid in task_moids]
    _log.info("vmware_tasks_recent_read", target=target.name, task_count=len(tasks))
    return {"tasks": tasks}


# ---------------------------------------------------------------------------
# Op metadata + schemas
# ---------------------------------------------------------------------------

_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "max_tasks": {
            "type": "integer",
            "minimum": 1,
            "maximum": _MAX_TASKS_CAP,
            "description": (
                f"Maximum number of recent tasks to return (1-{_MAX_TASKS_CAP}; "
                f"default {_DEFAULT_MAX_TASKS}). Caps how many of the "
                "TaskManager.recentTask MoRefs the info read fetches."
            ),
        },
    },
    "required": [],
    "additionalProperties": False,
}

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "Task moid."},
                    "operation": {
                        "type": ["string", "null"],
                        "description": "Operation identifier (``descriptionId``).",
                    },
                    "entity": {
                        "type": ["string", "null"],
                        "description": "Target entity moid (``entity``).",
                    },
                    "entity_type": {
                        "type": ["string", "null"],
                        "description": "Target entity type (``entity.type``).",
                    },
                    "entity_name": {
                        "type": ["string", "null"],
                        "description": "Target entity name (``entityName``).",
                    },
                    "state": {
                        "type": ["string", "null"],
                        "description": "Task state: queued / running / success / error.",
                    },
                    "progress": {
                        "type": ["integer", "null"],
                        "description": "Percent complete 0-100 (``progress``).",
                    },
                    "cancelled": {
                        "type": ["boolean", "null"],
                        "description": "Whether cancellation was requested (``cancelled``).",
                    },
                    "queue_time": {
                        "type": ["string", "null"],
                        "description": "When the task was created (``queueTime``).",
                    },
                    "start_time": {
                        "type": ["string", "null"],
                        "description": "When the task started (``startTime``).",
                    },
                    "complete_time": {
                        "type": ["string", "null"],
                        "description": "When the task completed (``completeTime``).",
                    },
                    "error_message": {
                        "type": ["string", "null"],
                        "description": "Localized fault message when state is error.",
                    },
                },
                "required": ["task"],
            },
            "description": "Recent tasks, most recent as vCenter orders them.",
        },
    },
    "required": ["tasks"],
}

#: Curated ``when_to_use`` blurb for the tasks-recent group.
TASKS_RECENT_WHEN_TO_USE = (
    "Use to read the recent vCenter Task objects for change-window "
    "monitoring: what operations ran, on which entity, their state "
    "(queued / running / success / error), progress, and the queued / "
    "started / completed timestamps. The 'what changed here, and did it "
    "finish?' read -- distinct from event.tail, which reads the event "
    "log, not the Task list. Reads TaskManager.recentTask then Task.info "
    "via PropertyCollector directly on the connector session, so it works "
    "with zero catalog ingest. Read-only."
)

VMWARE_TASKS_RECENT_OP = VmwareTypedOp(
    op_id="vmware.tasks.recent",
    handler_attr="tasks_recent",
    summary="Recent vCenter tasks with entity, state, progress, and timing.",
    description=(
        "Returns the recent vCenter Task objects (from TaskManager."
        "recentTask, capped at max_tasks) with each task's operation "
        "(descriptionId), target entity + type + name, state (queued / "
        "running / success / error), progress, cancelled flag, and the "
        "queue / start / complete timestamps -- plus the localized error "
        "message when a task faulted. The change-window monitoring read: "
        "'what changed here, and did it finish?', distinct from event.tail "
        "which reads the event log rather than the Task list. Reads "
        "TaskManager.recentTask then Task.info via PropertyCollector "
        "directly on the connector session, so it works with zero catalog "
        "ingest. safety_level=safe, read-only."
    ),
    parameter_schema=_PARAMETER_SCHEMA,
    response_schema=_RESPONSE_SCHEMA,
    group_key=TASKS_RECENT_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "tasks", "change-window"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call when the operator asks what recently changed on a vCenter, "
            "whether an operation finished / failed, or wants the recent task "
            "history. For the event log (logins, alarms, config-change "
            "events) use event.tail instead."
        ),
        "parameter_hints": {
            "max_tasks": (
                f"Cap on tasks returned (1-{_MAX_TASKS_CAP}; default {_DEFAULT_MAX_TASKS})."
            ),
        },
        "output_shape": (
            "{tasks: [{task, operation, entity, entity_type, entity_name, "
            "state, progress, cancelled, queue_time, start_time, "
            "complete_time, error_message}, ...]}."
        ),
    },
)
