# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.task.info`` typed op: one vCenter task by its id.

The status read of a change that vCenter runs as a task after the start call
returned (for example ``vmware.composite.host.add``). It reads ``Task.info``
with one ``RetrievePropertiesEx`` on the connector session, so it works with
no catalog ingest.

MEHO keeps no task state between calls; the caller keeps the task id.

* ``state`` is the vendor's task state: ``queued``, ``running``, ``success``
  or ``error``, with the progress, the vendor's error text and the object the
  task made (``result``, for example the new host).
* A task vCenter no longer knows gives ``not_found``. vCenter forgets
  finished tasks after a while.
* When vCenter does not answer (for example during its own upgrade), the
  answer is ``not_reachable`` with the task id and the time of the read. The
  same holds when a gateway answers HTTP 502, 503 or 504 for it. It never
  says ``error`` for that.
* An expired login is handled by the connector: on ``NotAuthenticated`` it
  logs in again and reads once more (``VmwareRestConnector._post_vmomi_json``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.base import ConnectorResourceNotFoundError
from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike
from meho_backplane.connectors.vmware_rest.typed_ops import (
    VmwareTypedOp,
    _int_or_none,
    _unwrap_value,
)
from meho_backplane.connectors.vmware_rest.typed_ops_tasks_recent import (
    TASKS_RECENT_GROUP_KEY,
    build_task_info_retrieve_params,
)
from meho_backplane.connectors.vmware_rest.vcenter_deployment import (
    now_iso,
    unreachable_http_reason,
    unreachable_reason,
)
from meho_backplane.connectors.vmware_rest.vim_body import (
    fault_message,
    fault_type_name,
    method_fault_type,
    unwrap_vim_value,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "TASK_INFO_OP_ID",
    "TASK_MOID_PATTERN",
    "VMWARE_TASK_INFO_OP",
    "task_info_impl",
]

_log = structlog.get_logger(__name__)

TASK_INFO_OP_ID: Final = "vmware.task.info"

#: A vCenter task id (``task-123``); a single ESXi host names tasks
#: ``haTask-...``. The id goes into a request body, never a URL path.
TASK_MOID_PATTERN: Final = r"^[A-Za-z][A-Za-z0-9._-]{0,254}$(?!\n)"

_RETRIEVE_PROPERTIES_PATH: Final = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"
_PROP_INFO: Final = "info"
_MO_NOT_FOUND_FAULT: Final = "ManagedObjectNotFound"

_STATES: Final = ("queued", "running", "success", "error", "not_found", "not_reachable")

_GUIDANCE: Final[dict[str, str]] = {
    "queued": "The task waits to start. Read it again in a few seconds.",
    "running": "The task runs. Read it again until the state is success or error.",
    "success": "The task is done. result names the object it made or changed, if any.",
    "error": "The task failed. error_message and error_type give the vendor's reason.",
    "not_found": (
        "vCenter does not know this task (any more). vCenter forgets finished tasks after a "
        "while. Read the object the task worked on to see its state."
    ),
    "not_reachable": (
        "vCenter did not answer, or a gateway answered for it. This is not a task failure. "
        "Read again in a few minutes with the same task id."
    ),
}


def _empty(task: str) -> dict[str, Any]:
    return {
        "task": task,
        "checked_at": now_iso(),
        "reachable": True,
        "state": "not_found",
        "operation": None,
        "entity": None,
        "entity_type": None,
        "entity_name": None,
        "progress": None,
        "cancelled": None,
        "queue_time": None,
        "start_time": None,
        "complete_time": None,
        "error_message": None,
        "error_type": None,
        "result": None,
        "unreachable_reason": None,
    }


def _task_info(payload: Any, task: str) -> dict[str, Any] | None:
    """The ``TaskInfo`` of *task* in a ``RetrievePropertiesEx`` answer, else ``None``."""
    body = _unwrap_value(payload)
    objects = body.get("objects", []) if isinstance(body, dict) else body
    for obj in objects if isinstance(objects, list) else []:
        if not isinstance(obj, dict):
            continue
        ref = obj.get("obj")
        if isinstance(ref, dict) and ref.get("value") not in (None, task):
            continue
        for prop in obj.get("propSet") or []:
            if isinstance(prop, dict) and prop.get("name") == _PROP_INFO:
                info = unwrap_vim_value(prop.get("val"))
                return info if isinstance(info, dict) else None
    return None


def _moref(value: Any) -> dict[str, str] | None:
    """``{type, value}`` of a vim MoRef result, else ``None``."""
    if isinstance(value, dict) and isinstance(value.get("value"), str):
        return {"type": str(value.get("type") or ""), "value": value["value"]}
    return None


def _project(answer: dict[str, Any], info: dict[str, Any]) -> None:
    entity = info.get("entity")
    state = info.get("state")
    failed = state == "error"
    answer.update(
        state=state if state in _STATES else "running",
        operation=info.get("descriptionId"),
        entity=entity.get("value") if isinstance(entity, dict) else None,
        entity_type=entity.get("type") if isinstance(entity, dict) else None,
        entity_name=info.get("entityName"),
        progress=_int_or_none(info.get("progress")),
        cancelled=info.get("cancelled") if isinstance(info.get("cancelled"), bool) else None,
        queue_time=info.get("queueTime"),
        start_time=info.get("startTime"),
        complete_time=info.get("completeTime"),
        error_message=fault_message(info.get("error")) if failed else None,
        error_type=fault_type_name(info.get("error")) if failed else None,
        result=_moref(info.get("result")) if state == "success" else None,
    )


def _not_reachable(
    answer: dict[str, Any], target: VsphereTargetLike, reason: str
) -> dict[str, Any]:
    """The answer when vCenter gave no answer: the task id and the time stay in it."""
    _log.info(
        "vmware_task_info_not_reachable", target=target.name, task=answer["task"], reason=reason
    )
    answer.update(reachable=False, state="not_reachable", unreachable_reason=reason)
    answer["guidance"] = _GUIDANCE["not_reachable"]
    return answer


async def task_info_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.task.info``: read ``Task.info`` of one task."""
    task = str(params["task"])
    answer = _empty(task)
    try:
        payload = await connector._post_vmomi_json(
            target,
            _RETRIEVE_PROPERTIES_PATH,
            operator=operator,
            json=build_task_info_retrieve_params([task]),
            promote_managed_object_not_found=True,
        )
    except ConnectorResourceNotFoundError:
        answer["guidance"] = _GUIDANCE["not_found"]
        return answer
    except httpx.TransportError as exc:
        return _not_reachable(answer, target, unreachable_reason(exc))
    except httpx.HTTPStatusError as exc:
        outage = unreachable_http_reason(exc)
        if outage is not None:
            return _not_reachable(answer, target, outage)
        if exc.response.status_code != 500 or (
            method_fault_type(exc.response.text) != _MO_NOT_FOUND_FAULT
        ):
            raise
        answer["guidance"] = _GUIDANCE["not_found"]
        return answer
    info = _task_info(payload, task)
    if info is not None:
        _project(answer, info)
    answer["guidance"] = _GUIDANCE[answer["state"]]
    _log.info("vmware_task_info_read", target=target.name, task=task, state=answer["state"])
    return answer


# ---------------------------------------------------------------------------
# Op metadata + schemas
# ---------------------------------------------------------------------------

_NULLABLE_TEXT: Final = ["string", "null"]

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task": {"type": "string", "description": "The task id that was read."},
        "checked_at": {"type": "string", "description": "When MEHO read it (ISO 8601, UTC)."},
        "reachable": {"type": "boolean", "description": "False when vCenter did not answer."},
        "state": {
            "type": "string",
            "enum": list(_STATES),
            "description": (
                "The vendor's task state (queued, running, success, error), not_found (vCenter "
                "does not know the task) or not_reachable (no answer; not a task failure)."
            ),
        },
        "operation": {
            "type": _NULLABLE_TEXT,
            "description": "What the task does, e.g. ClusterComputeResource.addHost.",
        },
        "entity": {"type": _NULLABLE_TEXT, "description": "Id of the object the task runs on."},
        "entity_type": {"type": _NULLABLE_TEXT, "description": "Type of that object."},
        "entity_name": {"type": _NULLABLE_TEXT, "description": "Name of that object."},
        "progress": {
            "type": ["integer", "null"],
            "description": "Progress in percent, when vCenter gives it.",
        },
        "cancelled": {"type": ["boolean", "null"], "description": "True when it was cancelled."},
        "queue_time": {"type": _NULLABLE_TEXT, "description": "When the task was queued."},
        "start_time": {"type": _NULLABLE_TEXT, "description": "When the task started."},
        "complete_time": {"type": _NULLABLE_TEXT, "description": "When the task ended."},
        "error_message": {
            "type": _NULLABLE_TEXT,
            "description": "The vendor's error text when the state is error.",
        },
        "error_type": {
            "type": _NULLABLE_TEXT,
            "description": "The vendor's fault type when the state is error, e.g. InvalidLogin.",
        },
        "result": {
            "type": ["object", "null"],
            "description": "The object the task made or changed, on success (if any).",
            "properties": {
                "type": {"type": "string", "description": "Type, e.g. HostSystem."},
                "value": {"type": "string", "description": "Id, e.g. host-1001."},
            },
        },
        "unreachable_reason": {
            "type": _NULLABLE_TEXT,
            "enum": [
                "connect_failed",
                "timeout",
                "tls_verify_failed",
                "service_unavailable",
                "gateway_error",
                None,
            ],
            "description": (
                "Why vCenter did not answer: no connection, a timeout, a failed TLS check, "
                "or a gateway answered HTTP 503 (service_unavailable), 502 or 504 "
                "(gateway_error). Null when vCenter answered."
            ),
        },
        "guidance": {"type": "string", "description": "What to do next, in plain words."},
    },
    "required": ["task", "checked_at", "reachable", "state"],
}

VMWARE_TASK_INFO_OP = VmwareTypedOp(
    op_id=TASK_INFO_OP_ID,
    handler_attr="task_info",
    summary="Read one vCenter task by id: state, progress, error and the object it made.",
    description=(
        "Returns the state of one vCenter task (queued, running, success, error), its "
        "progress, the vendor's error text, its times and the object it made. A task vCenter "
        "forgot gives not_found; no answer from vCenter gives not_reachable, never error."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "pattern": TASK_MOID_PATTERN,
                "description": "The task id a start call returned, e.g. task-1001.",
            },
        },
        "required": ["task"],
        "additionalProperties": False,
        "examples": [{"task": "task-1001"}],
    },
    response_schema=_RESPONSE_SCHEMA,
    group_key=TASKS_RECENT_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "tasks"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call after a start call that answered accepted with a task id (for example "
            "vmware.composite.host.add), until the state is success or error. Also call it "
            "after a lost reply, before you start the change again."
        ),
        "parameter_hints": {"task": "The task id from the start call's answer."},
        "output_shape": (
            "{task, checked_at, reachable, state: queued|running|success|error|not_found|"
            "not_reachable, operation, entity, entity_type, entity_name, progress, cancelled, "
            "queue_time, start_time, complete_time, error_message, error_type, result: "
            "{type, value}|null, unreachable_reason, guidance}."
        ),
        "next_step": (
            "queued, running or not_reachable: read again in a few seconds to minutes. "
            "success: read the object in result. error: report error_message; fix the cause "
            "before you start again. not_found: read the object the task worked on."
        ),
    },
)
