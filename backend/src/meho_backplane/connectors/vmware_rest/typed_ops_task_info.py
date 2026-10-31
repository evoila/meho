# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.task.info`` typed op -- the state of one vCenter task by its id.

The status read of a vendor job: a change such as
``vmware.composite.host.maintenance.enter`` answers ``accepted`` with the
vCenter task id, and the caller reads this op until the task ends.

One ``RetrievePropertiesEx`` of ``Task.info`` on the connector session. It
answers with the vendor's state (``queued`` / ``running`` / ``success`` /
``error``), the progress, the error type and text of a failed task, the
object and the times. Two more states, neither a failure of the task:

* ``not_found`` -- vCenter no longer knows the task (it keeps tasks for a
  limited time) or the id is wrong.
* ``not_reachable`` -- vCenter did not answer (for example during its own
  upgrade). The answer keeps the task id and the time of the read; MEHO keeps
  no job state between calls.

A login that expired since the start is renewed by the connector's VI-JSON
seam (``_post_vmomi_json`` logs in again on ``NotAuthenticated``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp
from meho_backplane.connectors.vmware_rest.typed_ops_tasks_recent import (
    TASKS_RECENT_GROUP_KEY,
    _parse_task_info,
)
from meho_backplane.connectors.vmware_rest.vcenter_deployment import now_iso, unreachable_reason
from meho_backplane.connectors.vmware_rest.vim_body import fault_type_name

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = ["VMWARE_TASK_INFO_OP", "task_info_impl"]

_log = structlog.get_logger(__name__)

_TASK_MO_TYPE: Final = "Task"
_TERMINAL: Final = frozenset({"success", "error"})
_VENDOR_STATES: Final = frozenset({"queued", "running", *_TERMINAL})

_GUIDANCE: Final[dict[str, str]] = {
    "queued": "The task waits to start. Read it again in a minute.",
    "running": "The task runs. Read it again in a minute.",
    "success": "The task ended well. Read the changed object to see its new state.",
    "error": "The task failed; error_message says why. Fix the cause before you try again.",
    "not_found": (
        "vCenter does not know this task id (wrong id, or an old task vCenter dropped). "
        "Read the object itself, or vmware.tasks.recent."
    ),
    "not_reachable": (
        "vCenter did not answer. This is not a failure of the task. Read again in a few "
        "minutes with the same task id."
    ),
}


def _answer(task: str, state: str) -> dict[str, Any]:
    return {
        "task": task,
        "checked_at": now_iso(),
        "reachable": state != "not_reachable",
        "state": state,
        "done": state in _TERMINAL,
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
        "unreachable_reason": None,
        "guidance": _GUIDANCE[state],
    }


async def task_info_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.task.info``: one read of ``Task.info``."""
    from meho_backplane.connectors.vmware_rest.composites._teardown_reads import read_object

    task = str(params["task"])
    try:
        props = await read_object(
            connector, target, operator, mo_type=_TASK_MO_TYPE, moid=task, props=["info"]
        )
    except httpx.TransportError as exc:
        answer = _answer(task, "not_reachable")
        answer["unreachable_reason"] = unreachable_reason(exc)
        _log.info("vmware_task_info_not_reachable", target=target.name, task=task)
        return answer
    info = props.get("info") if props is not None else None
    if not isinstance(info, dict):
        return _answer(task, "not_found")
    row = _parse_task_info(task, info)
    state = row.pop("state")
    answer = _answer(task, state if state in _VENDOR_STATES else "running")
    answer.update(row)
    if answer["state"] == "error":
        answer["error_type"] = fault_type_name(info.get("error"))
    _log.info("vmware_task_info_read", target=target.name, task=task, state=answer["state"])
    return answer


_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
            "description": "The vCenter task id a change returned, for example task-1234.",
        },
    },
    "required": ["task"],
    "additionalProperties": False,
    "examples": [{"task": "<task-id>"}],
}

_NULLABLE_STR: Final[dict[str, Any]] = {"type": ["string", "null"]}

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task": {"type": "string", "description": "The task id that was read."},
        "checked_at": {"type": "string", "description": "When MEHO read it (ISO 8601, UTC)."},
        "reachable": {"type": "boolean", "description": "False when vCenter did not answer."},
        "state": {
            "type": "string",
            "enum": ["queued", "running", "success", "error", "not_found", "not_reachable"],
            "description": (
                "The vendor state (queued, running, success, error), not_found (vCenter no "
                "longer knows the task) or not_reachable (no answer; not a failure)."
            ),
        },
        "done": {"type": "boolean", "description": "True on success or error."},
        "operation": {**_NULLABLE_STR, "description": "What the task does (descriptionId)."},
        "entity": {**_NULLABLE_STR, "description": "The moid of the object the task changes."},
        "entity_type": {**_NULLABLE_STR, "description": "The type of that object."},
        "entity_name": {**_NULLABLE_STR, "description": "The name of that object."},
        "progress": {"type": ["integer", "null"], "description": "Percent done, when given."},
        "cancelled": {"type": ["boolean", "null"], "description": "Whether a cancel was asked."},
        "queue_time": {**_NULLABLE_STR, "description": "When the task was created."},
        "start_time": {**_NULLABLE_STR, "description": "When the task started."},
        "complete_time": {**_NULLABLE_STR, "description": "When the task ended."},
        "error_message": {**_NULLABLE_STR, "description": "The vendor's error text on error."},
        "error_type": {**_NULLABLE_STR, "description": "The vendor's fault type on error."},
        "unreachable_reason": {
            "type": ["string", "null"],
            "enum": ["connect_failed", "timeout", "tls_verify_failed", None],
            "description": "Why vCenter did not answer; null when it answered.",
        },
        "guidance": {"type": "string", "description": "What to do next, in plain words."},
    },
    "required": ["task", "checked_at", "reachable", "state", "done", "guidance"],
}

VMWARE_TASK_INFO_OP = VmwareTypedOp(
    op_id="vmware.task.info",
    handler_attr="task_info",
    summary="Read one vCenter task by its id: state, progress, error, object and times.",
    description=(
        "Returns the state of one vCenter task (queued, running, success, error), the "
        "progress, the fault type and text of a failed task, the object and the times. "
        "Gives not_found for an unknown id and not_reachable when vCenter does not answer."
    ),
    parameter_schema=_PARAMETER_SCHEMA,
    response_schema=_RESPONSE_SCHEMA,
    group_key=TASKS_RECENT_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "tasks"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to follow a vCenter task a change started, with the task id from its "
            "answer (for example the status_read of host.maintenance.enter), until done is "
            "true. Also call it after a lost reply, before you start a change again."
        ),
        "parameter_hints": {"task": "The task id, for example task-1234."},
        "output_shape": (
            "{task, checked_at, reachable, state: queued|running|success|error|not_found|"
            "not_reachable, done, operation, entity, entity_type, entity_name, progress, "
            "cancelled, queue_time, start_time, complete_time, error_message, error_type, "
            "unreachable_reason, guidance}."
        ),
        "next_step": (
            "queued, running or not_reachable: read again in a minute. success: read the "
            "changed object. error: report error_message. not_found: read the object or "
            "vmware.tasks.recent."
        ),
    },
)
