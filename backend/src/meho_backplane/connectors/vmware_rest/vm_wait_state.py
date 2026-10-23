# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What a VM waits on: its pending question and its running vCenter tasks.

Shared by the ``vmware.vm.question.*`` typed operations and by
``vmware.composite.vm.device.cdrom``. When a device change times out, vCenter
may still run it, often because it waits on a question in the VM (for example
a locked CD-ROM door). :func:`device_change_result_unknown` turns that timeout
into a ``result_unknown`` answer that names the running tasks and the
question, so the caller does not start the same change again.

Both reads use ``RetrievePropertiesEx`` on the VI-JSON base through
:meth:`VmwareRestConnector._post_vmomi_json`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vmware_rest.composite_issues import issue
from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike
from meho_backplane.connectors.vmware_rest.typed_ops import _int_or_none, _unwrap_value
from meho_backplane.connectors.vmware_rest.typed_ops_tasks_recent import (
    build_task_info_retrieve_params,
)
from meho_backplane.connectors.vmware_rest.typed_ops_vm_info import _extract_vm_props
from meho_backplane.connectors.vmware_rest.vim_body import (
    retrieve_properties_body,
    unwrap_vim_value,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = ["device_change_result_unknown", "read_vm_wait_state"]

_log = structlog.get_logger(__name__)

_RETRIEVE_PROPERTIES_PATH: Final = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"
_VIRTUAL_MACHINE_MO_TYPE: Final = "VirtualMachine"
_PROP_NAME: Final = "name"
_PROP_QUESTION: Final = "runtime.question"
_PROP_RECENT_TASK: Final = "recentTask"
_PROP_INFO: Final = "info"
_RUNNING_STATES: Final = frozenset({"queued", "running"})


def _parse_question(raw: Any) -> dict[str, Any] | None:
    """Flatten a ``VirtualMachineQuestionInfo`` into the operator-facing shape."""
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
        return None
    raw_choice = raw.get("choice")
    choice: dict[str, Any] = raw_choice if isinstance(raw_choice, dict) else {}
    choices = [
        {"key": item.get("key"), "label": item.get("label"), "summary": item.get("summary")}
        for item in choice.get("choiceInfo") or []
        if isinstance(item, dict) and isinstance(item.get("key"), str)
    ]
    default_index = _int_or_none(choice.get("defaultIndex"))
    default_choice = (
        choices[default_index]["key"]
        if default_index is not None and 0 <= default_index < len(choices)
        else None
    )
    messages = [
        {"id": item.get("id"), "text": item.get("text")}
        for item in raw.get("message") or []
        if isinstance(item, dict)
    ]
    return {
        "id": raw["id"],
        "text": raw.get("text"),
        "choices": choices,
        "default_choice": default_choice,
        "messages": messages,
    }


def _moids(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    found: list[str] = []
    for ref in raw:
        value = ref.get("value") if isinstance(ref, dict) else ref
        if isinstance(value, str):
            found.append(value)
    return found


def _task_infos(retrieve_result: Any) -> dict[str, dict[str, Any]]:
    payload = _unwrap_value(retrieve_result)
    objects = payload.get("objects", []) if isinstance(payload, dict) else payload
    infos: dict[str, dict[str, Any]] = {}
    for obj in objects if isinstance(objects, list) else []:
        if not isinstance(obj, dict) or not isinstance(obj.get("obj"), dict):
            continue
        moid = obj["obj"].get("value")
        for prop in obj.get("propSet", []) or []:
            if isinstance(prop, dict) and prop.get("name") == _PROP_INFO:
                info = unwrap_vim_value(prop.get("val"))
                if isinstance(moid, str) and isinstance(info, dict):
                    infos[moid] = info
    return infos


async def read_vm_wait_state(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    vm_moid: str,
) -> dict[str, Any]:
    """Read a VM's name, pending question and running tasks.

    One ``RetrievePropertiesEx`` reads ``name``, ``runtime.question`` and
    ``recentTask`` on the VM. When the VM has recent tasks, a second one reads
    their ``Task.info`` and keeps the ``queued`` and ``running`` ones. An unset
    ``runtime.question`` is left out of the answer by vCenter, so it reads as
    ``None``.
    """
    props = _extract_vm_props(
        await connector._post_vmomi_json(
            target,
            _RETRIEVE_PROPERTIES_PATH,
            operator=operator,
            json=retrieve_properties_body(
                _VIRTUAL_MACHINE_MO_TYPE,
                [vm_moid],
                (_PROP_NAME, _PROP_QUESTION, _PROP_RECENT_TASK),
            ),
            promote_managed_object_not_found=True,
        )
    )
    running: list[dict[str, Any]] = []
    task_moids = _moids(props.get(_PROP_RECENT_TASK))
    if task_moids:
        infos = _task_infos(
            await connector._post_vmomi_json(
                target,
                _RETRIEVE_PROPERTIES_PATH,
                operator=operator,
                json=build_task_info_retrieve_params(task_moids),
            )
        )
        for moid in task_moids:
            info = infos.get(moid, {})
            if info.get("state") in _RUNNING_STATES:
                running.append(
                    {
                        "task": moid,
                        "operation": info.get("descriptionId"),
                        "state": info.get("state"),
                        "progress": _int_or_none(info.get("progress")),
                        "start_time": info.get("startTime"),
                    }
                )
    question = _parse_question(props.get(_PROP_QUESTION))
    return {
        "vm": vm_moid,
        "name": props.get(_PROP_NAME),
        "pending": question is not None,
        "question": question,
        "running_tasks": running,
    }


async def device_change_result_unknown(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    vm_moid: str,
) -> dict[str, Any]:
    """The result fields of a VM device change that timed out: the outcome is unknown.

    vCenter may still run the change, often because it waits on a question in
    the VM. A plain timeout made the next try collide with the running task, so
    the result names the VM's running tasks and its pending question. The read
    is best effort: when it fails, the result still says the outcome is unknown.
    The ``error`` issue makes the dispatcher record the call as failed.
    """
    try:
        state = await read_vm_wait_state(connector, operator, target, vm_moid)
    except Exception as exc:
        _log.warning("vmware_vm_wait_state_read_failed", vm=vm_moid, error_type=type(exc).__name__)
        state = {"question": None, "running_tasks": []}
    tasks = ", ".join(t["task"] for t in state["running_tasks"]) or "none found"
    guidance = (
        f"The change did not answer in time and may still run (vCenter tasks: {tasks}). "
        "Do not start it again yet."
    )
    if state["question"] is not None:
        guidance += (
            " The VM waits on a question: show it to the user and answer it with "
            "vmware.vm.question.answer."
        )
    else:
        guidance += " Check the VM again with vmware.vm.question.get."
    return {
        "status": "result_unknown",
        "running_tasks": state["running_tasks"],
        "vm_question": state["question"],
        "guidance": guidance,
        "issues": [
            issue("connector", "error", "the device change timed out; its result is unknown")
        ],
    }
