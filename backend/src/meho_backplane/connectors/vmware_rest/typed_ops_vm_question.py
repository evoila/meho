# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read and answer the question a VM waits on.

vCenter sometimes stops a VM change and asks a question in the VM, for
example "The guest operating system has locked the CD-ROM door ...
Disconnect anyway and override the lock?". The change waits until someone
answers. Two typed operations cover this:

* ``vmware.vm.question.get`` (``safe``) reads ``VirtualMachine.runtime.question``
  and the VM's running tasks. The question read must succeed; the task read
  is best effort and reports its failure in ``tasks_error``.
* ``vmware.vm.question.answer`` (``dangerous``, waits for approval) calls
  ``VirtualMachine.AnswerVM``. It only answers the question the approver saw:
  it reads the question again and stops when the question id changed or the
  answer is not one of the choices.

Both calls use the VI-JSON base ``/sdk/vim25/{release}`` through
:meth:`VmwareRestConnector._post_vmomi_json`, not the ``/api`` mount.

The shared read lives in :mod:`.vm_wait_state`; ``vmware.composite.vm.device.cdrom``
uses it too, when its device change times out.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp
from meho_backplane.connectors.vmware_rest.typed_ops_vm_info import _resolve_vm_moid_by_name
from meho_backplane.connectors.vmware_rest.vim_body import method_fault_type
from meho_backplane.connectors.vmware_rest.vm_wait_state import read_vm_wait_state
from meho_backplane.operations._preview import (
    PreviewContext,
    preview_failure_reason,
    register_preview_builder,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "VMWARE_VM_QUESTION_ANSWER_OP",
    "VMWARE_VM_QUESTION_GET_OP",
    "VM_QUESTION_GROUP_KEY",
    "VM_QUESTION_WHEN_TO_USE",
    "vm_question_answer_impl",
    "vm_question_get_impl",
]

_log = structlog.get_logger(__name__)

#: vCenter answers ``AnswerVM`` with these faults when the question is no
#: longer open: someone else answered it, or the id is not this VM's question.
_QUESTION_GONE_FAULTS: Final = frozenset({"ConcurrentAccess", "InvalidArgument"})

#: A vCenter managed-object id: letters, digits and dashes (``vm-42``). The
#: answer call puts it in the URL path, so anything else is refused.
_MOID_RE: Final = re.compile(r"[A-Za-z0-9-]+")
_MOID_PATTERN: Final = r"^[A-Za-z0-9-]+$(?!\n)"

VM_QUESTION_GROUP_KEY: Final = "vmware-vm-question"


def _check_moid(vm: str) -> str:
    if not _MOID_RE.fullmatch(vm):
        raise ValueError(f"vm {vm!r} is not a vCenter managed-object id such as 'vm-42'")
    return vm


async def vm_question_get_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.vm.question.get``."""
    vm_moid = params.get("vm")
    if not isinstance(vm_moid, str):
        vm_moid = await _resolve_vm_moid_by_name(connector, operator, target, params["name"])
    state = await read_vm_wait_state(connector, operator, target, vm_moid)
    _log.info(
        "vmware_vm_question_read",
        target=target.name,
        vm=vm_moid,
        pending=state["pending"],
        running_tasks=len(state["running_tasks"]),
        tasks_read=state["tasks_error"] is None,
    )
    return state


def _answer_result(
    status: str,
    params: dict[str, Any],
    question: dict[str, Any] | None,
    guidance: str | None,
) -> dict[str, Any]:
    choices = question["choices"] if question is not None else []
    label = next((c["label"] for c in choices if c["key"] == params["answer"]), None)
    return {
        "status": status,
        "vm": params["vm"],
        "question_id": params["question_id"],
        "answer": params["answer"],
        "answer_label": label,
        "question_text": question["text"] if question is not None else None,
        "choices": choices,
        "guidance": guidance,
    }


async def vm_question_answer_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.vm.question.answer``.

    Reads the question again before the answer. A different question id, or
    no question, returns ``question_gone``; an answer that is not one of the
    choice keys returns ``invalid_answer``. In both cases nothing is sent.
    A question vCenter could not read raises, so nothing is sent either. A
    failed task read does not stop the answer.
    ``AnswerVM`` returns no data (HTTP 204). A ``ConcurrentAccess`` or
    ``InvalidArgument`` fault means the question closed in the meantime, so
    it also returns ``question_gone``.
    """
    vm_moid = _check_moid(params["vm"])
    state = await read_vm_wait_state(connector, operator, target, vm_moid)
    question = state["question"]
    if question is None or question["id"] != params["question_id"]:
        return _answer_result(
            "question_gone",
            params,
            question,
            "The VM no longer waits on this question. Nothing was sent. Read the VM "
            "again with vmware.vm.question.get.",
        )
    if params["answer"] not in {c["key"] for c in question["choices"]}:
        return _answer_result(
            "invalid_answer",
            params,
            question,
            "answer must be the key of one of choices. Nothing was sent.",
        )
    try:
        await connector._post_vmomi_json(
            target,
            f"/VirtualMachine/{quote(vm_moid, safe='')}/AnswerVM",
            operator=operator,
            json={"questionId": params["question_id"], "answerChoice": params["answer"]},
        )
    except httpx.HTTPStatusError as exc:
        if method_fault_type(exc.response.text) not in _QUESTION_GONE_FAULTS:
            raise
        return _answer_result(
            "question_gone",
            params,
            question,
            "vCenter says the question is already answered or closed. Read the VM "
            "again with vmware.vm.question.get.",
        )
    _log.info("vmware_vm_question_answered", target=target.name, vm=vm_moid)
    return _answer_result("answered", params, question, None)


async def vm_question_answer_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver the question text and what the chosen answer means."""
    if ctx.connector_instance is None:
        return None
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    vm = str(ctx.params.get("vm", ""))
    try:
        state = await read_vm_wait_state(connector, ctx.operator, ctx.target, _check_moid(vm))
    except Exception as exc:
        return {
            "summary": "MEHO could not read the question, so it cannot show it.",
            "vm": vm,
            "read_error": preview_failure_reason(exc),
        }
    question = state["question"]
    still_open = question is not None and question["id"] == ctx.params.get("question_id")
    label = next(
        (
            c["label"]
            for c in (question or {}).get("choices", [])
            if c["key"] == ctx.params.get("answer")
        ),
        None,
    )
    if not still_open:
        summary = "The VM no longer waits on this question; the answer will not be sent."
    else:
        summary = f"Answer {label or ctx.params.get('answer')!r} to: {question['text']}"
    return {
        "summary": summary,
        "vm": vm,
        "name": state["name"],
        "question_still_open": still_open,
        "question_text": question["text"] if question is not None else None,
        "answer": ctx.params.get("answer"),
        "answer_label": label,
        "running_tasks": state["running_tasks"],
        "tasks_error": state["tasks_error"],
    }


# ---------------------------------------------------------------------------
# Op metadata + schemas
# ---------------------------------------------------------------------------

_TASK_ROW_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "task": {"type": "string", "description": "Task moid, e.g. 'task-1234'."},
        "operation": {
            "type": ["string", "null"],
            "description": "What the task does (TaskInfo.descriptionId).",
        },
        "state": {"type": ["string", "null"], "description": "'queued' or 'running'."},
        "progress": {"type": ["integer", "null"], "description": "Progress in percent."},
        "start_time": {"type": ["string", "null"], "description": "When the task started."},
    },
}

_QUESTION_SCHEMA: Final[dict[str, Any]] = {
    "type": ["object", "null"],
    "description": "The pending question; null when the VM waits on none.",
    "properties": {
        "id": {"type": "string", "description": "Question id; pass it as question_id."},
        "text": {"type": ["string", "null"], "description": "The question text."},
        "choices": {
            "type": "array",
            "description": "The possible answers.",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Pass this as answer."},
                    "label": {"type": ["string", "null"], "description": "e.g. 'Yes'."},
                    "summary": {"type": ["string", "null"], "description": "Longer text."},
                },
            },
        },
        "default_choice": {
            "type": ["string", "null"],
            "description": "Key of the default answer.",
        },
        "messages": {
            "type": "array",
            "description": "The single messages that make up the question.",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": ["string", "null"], "description": "Message id."},
                    "text": {"type": ["string", "null"], "description": "Message text."},
                },
            },
        },
    },
}

_VM_MOID_FIELD: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 1,
    "pattern": _MOID_PATTERN,
    "description": "Managed-object id of the VM, e.g. 'vm-42'.",
}

_GET_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "vm": {**_VM_MOID_FIELD, "description": "VM moid, e.g. 'vm-42'. Give vm or name."},
        "name": {
            "type": "string",
            "minLength": 1,
            "description": "VM name. Give vm or name.",
        },
    },
    "oneOf": [{"required": ["vm"]}, {"required": ["name"]}],
    "additionalProperties": False,
    "examples": [{"vm": "vm-42"}],
}

_GET_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "vm": {"type": "string", "description": "VM moid."},
        "name": {"type": ["string", "null"], "description": "VM name."},
        "pending": {"type": "boolean", "description": "True when the VM waits on a question."},
        "question": _QUESTION_SCHEMA,
        "running_tasks": {
            "type": "array",
            "description": (
                "Queued or running vCenter tasks on this VM. Empty when tasks_error is set."
            ),
            "items": _TASK_ROW_SCHEMA,
        },
        "tasks_error": {
            "type": ["string", "null"],
            "description": (
                "Why the running tasks could not be read; null when they were read. "
                "The question is still valid."
            ),
        },
    },
    "required": ["vm", "pending", "question", "running_tasks", "tasks_error"],
}

#: Fields of ``question`` that stay inline when a long ``running_tasks`` list
#: makes JSONFlux reduce the result to a handle. Without this the caller would
#: lose the question id and choices it needs for ``vmware.vm.question.answer``.
#: The inline copy is bounded; the reducer also stores the whole ``question``
#: with the rows, so when ``result_object_truncations`` names it, a
#: ``result_query`` read with ``path="/question"`` returns every choice.
_GET_RESULT_OBJECTS: Final[dict[str, Any]] = {
    "objects": {"question": ["id", "text", "choices", "default_choice", "messages"]}
}

_ANSWER_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "vm": _VM_MOID_FIELD,
        "question_id": {
            "type": "string",
            "minLength": 1,
            "description": "question.id from vmware.vm.question.get.",
        },
        "answer": {
            "type": "string",
            "minLength": 1,
            "description": "The key of one of question.choices, e.g. '0'.",
        },
    },
    "required": ["vm", "question_id", "answer"],
    "additionalProperties": False,
    "examples": [{"vm": "vm-42", "question_id": "_vmx1", "answer": "0"}],
}

_ANSWER_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["answered", "question_gone", "invalid_answer"],
            "description": (
                "'answered': vCenter took the answer. 'question_gone': the VM no longer "
                "waits on this question. 'invalid_answer': answer is not a choice key. "
                "Only 'answered' sent anything."
            ),
        },
        "vm": {"type": "string", "description": "VM moid."},
        "question_id": {"type": "string", "description": "The question id that was given."},
        "answer": {"type": "string", "description": "The answer key that was given."},
        "answer_label": {"type": ["string", "null"], "description": "Label of that answer."},
        "question_text": {
            "type": ["string", "null"],
            "description": "Text of the question the VM waits on now.",
        },
        "choices": {
            "type": "array",
            "description": "The answers of the current question.",
            "items": _QUESTION_SCHEMA["properties"]["choices"]["items"],
        },
        "guidance": {"type": ["string", "null"], "description": "What to do next."},
    },
    "required": ["status", "vm", "question_id", "answer"],
}

VM_QUESTION_WHEN_TO_USE: Final = (
    "Use when a VM change hangs or times out: vCenter may wait on a question in the "
    "VM (for example a locked CD-ROM door). Read the question and the running tasks "
    "of the VM, then answer it. Answering waits for a person's approval."
)

VMWARE_VM_QUESTION_GET_OP = VmwareTypedOp(
    op_id="vmware.vm.question.get",
    handler_attr="vm_question_get",
    summary="The question a VM waits on, its answers, and the VM's running vCenter tasks.",
    description=(
        "Reads the pending question of one VM (runtime.question) with its possible "
        "answers, and the queued or running vCenter tasks on the VM. By moid (vm) or "
        "name. Read-only. Fails when vCenter cannot read the question; a failed task "
        "read is reported in tasks_error."
    ),
    parameter_schema=_GET_PARAMETER_SCHEMA,
    response_schema=_GET_RESPONSE_SCHEMA,
    group_key=VM_QUESTION_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "vm", "question", "vi-json"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call when a VM change timed out or hangs, or a later change fails with "
            "'Another task is already in progress'. vCenter may wait on a question in "
            "the VM until someone answers."
        ),
        "parameter_hints": {"vm": "VM moid, e.g. 'vm-42'.", "name": "VM name."},
        "output_shape": (
            "{vm, name, pending, question: {id, text, choices: [{key, label, summary}], "
            "default_choice, messages: [{id, text}]} | null, running_tasks: [{task, "
            "operation, state, progress, start_time}], tasks_error: string | null}."
        ),
        "next_step": (
            "When pending is true, show the question text and choices to the user, then "
            "call vmware.vm.question.answer with question.id and the chosen key. Do not "
            "start the same change again while running_tasks is not empty. When "
            "tasks_error is set, the running tasks are unknown: treat the change as "
            "still running."
        ),
        "result_objects": _GET_RESULT_OBJECTS,
    },
)

VMWARE_VM_QUESTION_ANSWER_OP = VmwareTypedOp(
    op_id="vmware.vm.question.answer",
    handler_attr="vm_question_answer",
    summary="Answer the question a VM waits on, so the waiting change can go on.",
    description=(
        "Sends one answer to the pending question of a VM (AnswerVM). It answers only "
        "the question with the given id, and only with one of its choice keys. Waits "
        "for approval."
    ),
    parameter_schema=_ANSWER_PARAMETER_SCHEMA,
    response_schema=_ANSWER_RESPONSE_SCHEMA,
    group_key=VM_QUESTION_GROUP_KEY,
    tags=("write", "vmware", "vcenter", "vm", "question", "vi-json"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call after vmware.vm.question.get shows a pending question and the user "
            "chose an answer."
        ),
        "preconditions": (
            "question_id and answer come from vmware.vm.question.get. The answer can "
            "change the VM: for example 'Yes' to a locked CD-ROM question disconnects "
            "the drive while the guest uses it. Let the user choose."
        ),
        "parameter_hints": {
            "vm": "VM moid, e.g. 'vm-42'.",
            "question_id": "question.id from vmware.vm.question.get.",
            "answer": "The key of the chosen answer (not its label).",
        },
        "output_shape": (
            "{status: answered|question_gone|invalid_answer, vm, question_id, answer, "
            "answer_label, question_text, choices, guidance}."
        ),
        "next_step": (
            "Call vmware.vm.question.get again: the waiting task should finish, or the "
            "VM may ask a new question. On question_gone, read the new state first."
        ),
    },
)

register_preview_builder(VMWARE_VM_QUESTION_ANSWER_OP.op_id, vm_question_answer_preview)

TYPED_OPS: tuple[VmwareTypedOp, ...] = (VMWARE_VM_QUESTION_GET_OP, VMWARE_VM_QUESTION_ANSWER_OP)
WHEN_TO_USE_BY_GROUP: dict[str, str] = {VM_QUESTION_GROUP_KEY: VM_QUESTION_WHEN_TO_USE}
