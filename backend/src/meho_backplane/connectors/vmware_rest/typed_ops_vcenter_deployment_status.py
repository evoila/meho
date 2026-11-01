# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.vcenter.deployment.status`` typed op (#4394).

The install state of a new vCenter appliance: not ready, ready, running,
waiting for an answer, failed or done. Also the progress, each setup step with
its error text, and the open question when there is one. It runs on the new
appliance as its own target (see :mod:`..vcenter_deployment`).

It is a ``source_kind="typed"`` bound method on :class:`VmwareRestConnector`.
MEHO keeps no install state between calls: each call reads the appliance.

* When the appliance does not answer (it restarts its services during the
  setup), the answer is ``state: not_reachable`` with the reason and the time
  of the read. It never says ``failed`` for that.
* An expired login gives HTTP 401: the dispatcher logs in again and runs the
  read once more, so the read still works.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp
from meho_backplane.connectors.vmware_rest.vcenter_deployment import (
    INSTALL_GROUP_KEY,
    INSTALL_WHEN_TO_USE,
    MEHO_STATES,
    STATUS_OP_ID,
    VENDOR_STATES,
    deployment_view,
    now_iso,
    read_deployment,
    read_questions,
    unreachable_reason,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "DEPLOYMENT_STATUS_GROUP_KEY",
    "DEPLOYMENT_STATUS_WHEN_TO_USE",
    "TYPED_OPS",
    "VMWARE_VCENTER_DEPLOYMENT_STATUS_OP",
    "WHEN_TO_USE_BY_GROUP",
    "deployment_status_impl",
]

_log = structlog.get_logger(__name__)

DEPLOYMENT_STATUS_GROUP_KEY = INSTALL_GROUP_KEY
DEPLOYMENT_STATUS_WHEN_TO_USE = INSTALL_WHEN_TO_USE

_GUIDANCE: dict[str, str] = {
    "not_ready": ("The appliance has not finished its first start. Read again in a few minutes."),
    "ready": "The setup has not started. Start it with vmware.composite.vcenter.install.",
    "running": "The setup runs. Read again until the state is done or failed.",
    "waiting_for_answer": (
        "The setup waits for an answer. Answer the question in questions with "
        "vmware.composite.vcenter.deployment.answer."
    ),
    "failed": (
        "The setup failed. The steps show the error. Fix the cause, then deploy a new "
        "appliance: MEHO has no operation that resets a failed one."
    ),
    "done": "The setup is done. The appliance is now a vCenter on port 443.",
    "unknown": "The appliance gave no install state. Read again in a few minutes.",
    "not_reachable": (
        "The appliance did not answer. This is normal while the setup restarts its "
        "services. Read again in a few minutes. It does not mean the setup failed."
    ),
}


def _empty(target: VsphereTargetLike, checked_at: str) -> dict[str, Any]:
    return {
        "target": target.name,
        "checked_at": checked_at,
        "reachable": True,
        "state": "unknown",
        "vendor_state": None,
        "task_status": None,
        "progress_percent": None,
        "progress_message": None,
        "error": None,
        "operation": None,
        "started_at": None,
        "ended_at": None,
        "steps": [],
        "questions": [],
        "unreachable_reason": None,
        "read_note": None,
    }


async def deployment_status_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.vcenter.deployment.status``.

    Reads ``GET /vcenter/deployment``, and ``GET /vcenter/deployment/question``
    when the appliance waits for an answer. A transport fault (no answer, a
    timeout, a failed TLS check) gives ``state: not_reachable``. HTTP 404 (the
    vendor's "state cannot be determined") gives ``state: unknown``. Other HTTP
    errors raise, so the dispatcher reports them (and logs in again on a 401).
    """
    del params
    answer = _empty(target, now_iso())
    try:
        view = deployment_view(await read_deployment(connector, target, operator))
    except httpx.TransportError as exc:
        reason = unreachable_reason(exc)
        _log.info("vcenter_deployment_status_not_reachable", target=target.name, reason=reason)
        answer.update(reachable=False, state="not_reachable", unreachable_reason=reason)
        answer["guidance"] = _GUIDANCE["not_reachable"]
        return answer
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
        answer["read_note"] = "the appliance answered HTTP 404: it cannot say its state now"
        answer["guidance"] = _GUIDANCE["unknown"]
        return answer
    answer.update(view)
    if view["vendor_state"] == "QUESTION_RAISED":
        try:
            answer["questions"] = await read_questions(connector, target, operator)
        except (httpx.HTTPStatusError, httpx.TransportError) as exc:
            answer["read_note"] = f"the question read failed ({type(exc).__name__})"
    answer["guidance"] = _GUIDANCE[answer["state"]]
    _log.info(
        "vcenter_deployment_status_read",
        target=target.name,
        state=answer["state"],
        step_count=len(answer["steps"]),
    )
    return answer


# ---------------------------------------------------------------------------
# Op metadata + schemas
# ---------------------------------------------------------------------------

_STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Vendor name of the setup step."},
        "status": {
            "type": ["string", "null"],
            "enum": ["PENDING", "RUNNING", "BLOCKED", "SUCCEEDED", "FAILED", None],
            "description": "Vendor status of the step.",
        },
        "progress_percent": {
            "type": ["integer", "null"],
            "description": "Progress of the step in percent; null when not given.",
        },
        "description": {"type": ["string", "null"], "description": "What the step does."},
        "error": {"type": ["string", "null"], "description": "Vendor error text of the step."},
        "errors": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Vendor error messages of the step, with the fix when given.",
        },
        "warnings": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Vendor warning messages of the step.",
        },
    },
    "required": ["name", "status", "errors"],
}

QUESTION_ROW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Question id; pass it to the answer operation."},
        "text": {"type": ["string", "null"], "description": "The question."},
        "type": {
            "type": ["string", "null"],
            "enum": ["YES_NO", "OK_CANCEL", "ABORT_RETRY_IGNORE", None],
            "description": "Kind of question.",
        },
        "default_answer": {"type": ["string", "null"], "description": "The vendor's default."},
        "possible_answers": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The answers the question accepts.",
        },
    },
    "required": ["id", "possible_answers"],
}

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target": {"type": "string", "description": "The appliance target that was read."},
        "checked_at": {"type": "string", "description": "When MEHO read it (ISO 8601, UTC)."},
        "reachable": {"type": "boolean", "description": "False when the appliance did not answer."},
        "state": {
            "type": "string",
            "enum": list(MEHO_STATES),
            "description": (
                "not_ready, ready (setup not started), running, waiting_for_answer, failed, "
                "done, unknown (no state given) or not_reachable (no answer; not a failure)."
            ),
        },
        "vendor_state": {
            "type": ["string", "null"],
            "enum": [*VENDOR_STATES, None],
            "description": "The vendor's appliance state.",
        },
        "task_status": {
            "type": ["string", "null"],
            "enum": ["PENDING", "RUNNING", "BLOCKED", "SUCCEEDED", "FAILED", None],
            "description": "Vendor status of the setup task.",
        },
        "progress_percent": {
            "type": ["integer", "null"],
            "description": "Progress of the setup in percent; null when not given.",
        },
        "progress_message": {
            "type": ["string", "null"],
            "description": "The vendor's progress text.",
        },
        "error": {"type": ["string", "null"], "description": "Vendor error text of the setup."},
        "operation": {"type": ["string", "null"], "description": "Vendor operation of the task."},
        "started_at": {"type": ["string", "null"], "description": "When the setup started."},
        "ended_at": {"type": ["string", "null"], "description": "When the setup ended."},
        "steps": {
            "type": "array",
            "items": _STEP_SCHEMA,
            "description": "One row per setup step, in the vendor's order.",
        },
        "questions": {
            "type": "array",
            "items": QUESTION_ROW_SCHEMA,
            "description": "The open questions; empty unless the state is waiting_for_answer.",
        },
        "unreachable_reason": {
            "type": ["string", "null"],
            "enum": ["connect_failed", "timeout", "tls_verify_failed", None],
            "description": "Why the appliance did not answer; null when it answered.",
        },
        "read_note": {
            "type": ["string", "null"],
            "description": "Which read failed; null when all reads worked.",
        },
        "guidance": {"type": "string", "description": "What to do next, in plain words."},
    },
    "required": ["target", "checked_at", "reachable", "state", "steps", "questions"],
}

_RESULT_SCALARS = [
    "target",
    "checked_at",
    "reachable",
    "state",
    "vendor_state",
    "task_status",
    "progress_percent",
    "progress_message",
    "error",
    "unreachable_reason",
    "read_note",
    "guidance",
]

VMWARE_VCENTER_DEPLOYMENT_STATUS_OP = VmwareTypedOp(
    op_id=STATUS_OP_ID,
    handler_attr="vcenter_deployment_status",
    summary="Read the install state of a new vCenter appliance, its steps and open question.",
    description=(
        "Returns the setup state of a new vCenter appliance (not ready, ready, running, "
        "waiting for an answer, failed, done), the progress, each step with its error text, "
        "and the open question. Runs on the new appliance target. No answer gives "
        "not_reachable, never failed."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
        "examples": [{}],
    },
    response_schema=_RESPONSE_SCHEMA,
    group_key=DEPLOYMENT_STATUS_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "vcenter-install"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call on the target of a new vCenter appliance: before "
            "vmware.composite.vcenter.install (the state must be ready), after it to follow "
            "the setup, and after a lost reply of a start or an answer, before you try again."
        ),
        "parameter_hints": {},
        "output_shape": (
            "{target, checked_at, reachable, state: not_ready|ready|running|"
            "waiting_for_answer|failed|done|unknown|not_reachable, vendor_state, task_status, "
            "progress_percent, progress_message, error, operation, started_at, ended_at, "
            "steps: [{name, status, progress_percent, description, error, errors, warnings}], "
            "questions: [{id, text, type, default_answer, possible_answers}], "
            "unreachable_reason, read_note, guidance}."
        ),
        "next_step": (
            "running or not_reachable: read again in a few minutes; the appliance restarts "
            "its services during the setup. waiting_for_answer: call "
            "vmware.composite.vcenter.deployment.answer with a question id and one of its "
            "possible_answers. failed: the steps say why; fix it and deploy a new "
            "appliance. done: add the new vCenter as a target on port 443."
        ),
        "result_scalars": {"keys": _RESULT_SCALARS},
    },
)

TYPED_OPS: tuple[VmwareTypedOp, ...] = (VMWARE_VCENTER_DEPLOYMENT_STATUS_OP,)
WHEN_TO_USE_BY_GROUP: dict[str, str] = {DEPLOYMENT_STATUS_GROUP_KEY: DEPLOYMENT_STATUS_WHEN_TO_USE}
