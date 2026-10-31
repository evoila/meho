# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Answer a question of a vCenter setup: ``vmware.composite.vcenter.deployment.answer`` (#4394).

A running setup can stop and ask a question (for example: trust this
certificate?). This change answers it, so the setup goes on. It runs on the
new appliance target, like ``vcenter.install``. ``dangerous`` with
``requires_approval=True``.

1. ``GET /vcenter/deployment``: the appliance must wait for an answer (vendor
   state ``QUESTION_RAISED``); else ``no_question`` and nothing is sent.
2. ``GET /vcenter/deployment/question``: the question id must be open
   (``unknown_question``), and the answer must be one of its
   ``possible_answers`` (``answer_not_allowed``).
3. ``POST /vcenter/deployment/question?action=answer`` with exactly the id and
   the answer the approver saw.

The vendor answers at once and goes on by itself: the answer is ``accepted``
with the status read that follows the setup. A lost reply gives
``answer_unknown``: read the status first; when the question is still open,
answer again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composite_issues import issue
from meho_backplane.connectors.vmware_rest.composites._write import (
    _WRITE_REQUIRES_APPROVAL,
    _WRITE_SAFETY_LEVEL,
    _step_connector_id,
)
from meho_backplane.connectors.vmware_rest.vcenter_deployment import (
    OP_DEPLOYMENT_GET,
    OP_QUESTION_ANSWER,
    OP_QUESTION_GET,
    StepLog,
    deployment_view,
    mounted,
    read_deployment,
    read_questions,
    status_read_ref,
    unreachable_reason,
    vendor_detail,
)
from meho_backplane.operations._preview import register_preview_builder
from meho_backplane.operations.composite import enforce_subop_policy

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "ANSWER_OP_ID",
    "vcenter_deployment_answer_composite",
    "vcenter_deployment_answer_preview",
]

ANSWER_OP_ID: Final = "vmware.composite.vcenter.deployment.answer"

_SUB_OPS_VCENTER_DEPLOYMENT_ANSWER: tuple[str, ...] = (
    OP_DEPLOYMENT_GET,
    OP_QUESTION_GET,
    OP_QUESTION_ANSWER,
)

_STEPS: Final = ("read_state", "read_question", "answer")


@dataclass
class _Run:
    """One answer call: its inputs and what it found so far."""

    target: Any
    question_id: str
    wanted: str
    steps: StepLog = field(default_factory=lambda: StepLog(_STEPS))
    question: dict[str, Any] | None = None

    def answer(self, status: str, guidance: str, error: str | None = None) -> dict[str, Any]:
        return {
            "status": status,
            "question_id": self.question_id,
            "answer": self.wanted,
            "question": self.question,
            "status_read": status_read_ref(self.target),
            "steps": self.steps.rows(),
            "issues": [issue("answer", "error", error)] if error else [],
            "guidance": guidance,
        }


async def _find_question(
    connector: VmwareRestConnector, operator: Operator, run: _Run
) -> dict[str, Any] | None:
    """Steps 1 and 2. ``None`` when the answer may be sent; else the answer that stops."""
    view = deployment_view(await read_deployment(connector, run.target, operator))
    run.steps.done("read_state")
    if view["vendor_state"] != "QUESTION_RAISED":
        return run.answer(
            "no_question",
            "Nothing was sent. Read vmware.vcenter.deployment.status for the state.",
            f"the appliance waits for no answer (state {view['vendor_state']!r})",
        )
    questions = await read_questions(connector, run.target, operator)
    run.steps.done("read_question")
    run.question = next((q for q in questions if q["id"] == run.question_id), None)
    if run.question is None:
        open_ids = ", ".join(q["id"] for q in questions) or "none"
        return run.answer(
            "unknown_question",
            "Nothing was sent. Take a question id from vmware.vcenter.deployment.status.",
            f"no open question has the id {run.question_id!r} (open: {open_ids})",
        )
    allowed = run.question["possible_answers"]
    if run.wanted not in allowed:
        return run.answer(
            "answer_not_allowed",
            "Nothing was sent. Use one of possible_answers.",
            f"{run.wanted!r} is not an answer of this question (allowed: {', '.join(allowed)})",
        )
    return None


async def _post_answer(
    connector: VmwareRestConnector, operator: Operator, run: _Run
) -> dict[str, Any]:
    """Step 3, after the gate: send the answer and say what happened."""
    path = await mounted(connector, run.target, operator, OP_QUESTION_ANSWER)
    body = {"question_id": run.question_id, "answer_val": run.wanted}
    try:
        await connector._post_json(run.target, path, operator=operator, json=body)
    except httpx.HTTPStatusError as exc:
        detail = vendor_detail(exc, ())
        run.steps.failed("answer", detail)
        return run.answer(
            "answer_failed",
            "Read vmware.vcenter.deployment.status before you try again.",
            f"the appliance refused the answer: {detail}",
        )
    except httpx.TransportError as exc:
        reason = unreachable_reason(exc)
        run.steps.unknown("answer", f"the answer got no reply ({reason})")
        return run.answer(
            "answer_unknown",
            "Do not answer again blindly. Read vmware.vcenter.deployment.status: if the "
            "question is still open, answer again.",
            f"the answer got no reply ({reason}); MEHO does not know if it arrived",
        )
    run.steps.done("answer")
    return run.answer(
        "accepted",
        "The appliance accepted the answer and goes on with the setup. Read "
        "vmware.vcenter.deployment.status until the state is done or failed.",
    )


async def vcenter_deployment_answer_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Answer the open question of a running setup, so the setup goes on.

    Op-id: ``vmware.composite.vcenter.deployment.answer``. See the module
    docstring for the flow. A parked or denied gate comes back as is.
    """
    run = _Run(target=target, question_id=str(params["question_id"]), wanted=str(params["answer"]))
    stop = await _find_question(connector, operator, run)
    if stop is not None:
        return stop
    gate = await enforce_subop_policy(
        operator=operator,
        connector_id=_step_connector_id(connector),
        op_id=OP_QUESTION_ANSWER,
        safety_level=_WRITE_SAFETY_LEVEL,
        requires_approval=_WRITE_REQUIRES_APPROVAL,
        target=target,
        params={"question_id": run.question_id, "answer_val": run.wanted},
    )
    if gate is not None:
        return gate
    return await _post_answer(connector, operator, run)


async def vcenter_deployment_answer_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """What the approver sees: the question id and the answer MEHO will send."""
    question_id, answer = ctx.params.get("question_id"), ctx.params.get("answer")
    if not isinstance(question_id, str) or not isinstance(answer, str):
        return None
    return {
        "action": "answer the open question of the vCenter setup",
        "target": str(getattr(ctx.target, "name", "")),
        "question_id": question_id,
        "answer": answer,
        "note": "MEHO sends the answer only if this question is still open and allows it.",
    }


register_preview_builder(ANSWER_OP_ID, vcenter_deployment_answer_preview)
