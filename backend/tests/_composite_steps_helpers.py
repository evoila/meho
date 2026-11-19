# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Multi-step operations for ``test_operations_composite_steps_resume.py``.

Each operation makes a change, then reaches a step that waits for a person.
Every change a step sends is added to :data:`sent`, so a test can count the
requests that reached the target. The dispatcher imports these handlers by
their module path (``handler_ref``); closures are rejected.

Connector, operator and registration come from
``_composite_wait_audit_helpers``.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.auth.operator import Operator
from meho_backplane.operations import DispatchChild, register_typed_operation
from meho_backplane.operations.composite import enforce_subop_policy, run_governed_subop

from . import _composite_wait_audit_helpers as h

#: Every change sent to the target, in order.
sent: list[str] = []

CHANGE_CHILD_OP_ID = "vault.things.write"
CHANGE_STEP = "POST:/v1/sys/things"
SECOND_CHANGE_STEP = "PUT:/v1/sys/things/{id}"
CREDENTIAL_STEP = "POST:/v1/sys/credentials"
SECOND_WAIT_STEP = "POST:/v1/sys/things/{id}/seal"

CHILD_THEN_WAIT_OP_ID = "vault.composite.child_change_then_wait"
GATED_THEN_WAIT_OP_ID = "vault.composite.gated_change_then_wait"
STEP_THEN_CREDENTIAL_OP_ID = "vault.composite.change_then_credential"
TWO_WAITS_OP_ID = "vault.composite.change_wait_change_wait"
LOST_END_OP_ID = "vault.composite.change_without_end_then_wait"
POP_RESULT_OP_ID = "vault.composite.change_pop_result_wait_wait"


async def change_child_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Typed child step: one change on the target."""
    sent.append(CHANGE_CHILD_OP_ID)
    return {"version": 1}


async def register_change_child(embedding: Any) -> None:
    """Register the typed change step run through ``dispatch_child``."""
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=CHANGE_CHILD_OP_ID,
        handler=change_child_handler,
        summary="Write metadata.",
        description="Write metadata.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        safety_level="safe",
        embedding_service=embedding,
    )


async def _waiting_step(operator: Operator, target: Any, op_id: str, params: dict[str, Any]) -> Any:
    """A direct step that waits for a person; records itself when it runs."""

    async def _send() -> dict[str, Any]:
        sent.append(op_id)
        return {"ok": True}

    return await run_governed_subop(
        operator=operator,
        connector_id=h.STEP_CONNECTOR_ID,
        op_id=op_id,
        safety_level="safe",
        requires_approval=True,
        target=target,
        params=params,
        call=_send,
    )


async def _change_step(
    operator: Operator, target: Any, op_id: str, params: dict[str, Any], value: dict[str, Any]
) -> Any:
    """A direct change that goes ahead without a wait; returns *value*."""

    async def _send() -> dict[str, Any]:
        sent.append(op_id)
        return value

    return await run_governed_subop(
        operator=operator,
        connector_id=h.STEP_CONNECTOR_ID,
        op_id=op_id,
        safety_level="safe",
        requires_approval=False,
        target=target,
        params=params,
        call=_send,
    )


async def child_change_then_wait_composite(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: DispatchChild
) -> Any:
    """A change run through ``dispatch_child``, then a step that waits."""
    changed = await dispatch_child(
        connector_id=h.CONNECTOR_ID, op_id=CHANGE_CHILD_OP_ID, params={"path": "app"}
    )
    if changed.status != "ok":
        return changed
    gated = await enforce_subop_policy(
        operator=operator,
        connector_id=h.STEP_CONNECTOR_ID,
        op_id=CREDENTIAL_STEP,
        safety_level="safe",
        requires_approval=True,
        target=target,
        params={"path": "app"},
    )
    if gated is not None:
        return gated
    sent.append(CREDENTIAL_STEP)
    return {"done": True}


async def gated_change_then_wait_composite(
    operator: Operator, target: Any, params: dict[str, Any]
) -> Any:
    """A change checked only by the gate (the handler sends it), then a step that waits."""
    gated = await enforce_subop_policy(
        operator=operator,
        connector_id=h.STEP_CONNECTOR_ID,
        op_id=CHANGE_STEP,
        safety_level="safe",
        requires_approval=False,
        target=target,
        params={"name": "thing-1"},
    )
    if gated is not None:
        return gated
    sent.append(CHANGE_STEP)
    waited = await _waiting_step(operator, target, CREDENTIAL_STEP, {"path": "app"})
    return waited.stopped if waited.stopped is not None else {"done": True}


async def change_then_credential_composite(
    operator: Operator, target: Any, params: dict[str, Any]
) -> Any:
    """A change, then a credential step that waits for a person."""
    made = await _change_step(operator, target, CHANGE_STEP, {"name": "thing-1"}, {"id": "t-1"})
    if made.stopped is not None:
        return made.stopped
    waited = await _waiting_step(operator, target, CREDENTIAL_STEP, {"path": made.value["id"]})
    if waited.stopped is not None:
        return waited.stopped
    return {"done": True, "id": made.value["id"]}


async def change_wait_change_wait_composite(
    operator: Operator, target: Any, params: dict[str, Any]
) -> Any:
    """Change, wait, change, wait: two steps of one run wait for a person."""
    made = await _change_step(operator, target, CHANGE_STEP, {"name": "thing-1"}, {"id": "t-1"})
    if made.stopped is not None:
        return made.stopped
    thing = {"id": made.value["id"]}
    first = await _waiting_step(operator, target, CREDENTIAL_STEP, {"path": thing["id"]})
    if first.stopped is not None:
        return first.stopped
    changed = await _change_step(operator, target, SECOND_CHANGE_STEP, thing, {"changed": True})
    if changed.stopped is not None:
        return changed.stopped
    second = await _waiting_step(operator, target, SECOND_WAIT_STEP, thing)
    if second.stopped is not None:
        return second.stopped
    return {"done": True}


async def change_without_end_then_wait_composite(
    operator: Operator, target: Any, params: dict[str, Any]
) -> Any:
    """A change whose call fails after it was sent (no stored end), then a step that waits."""

    async def _send_then_time_out() -> dict[str, Any]:
        sent.append(CHANGE_STEP)
        raise TimeoutError("no answer from the target")

    try:
        made = await run_governed_subop(
            operator=operator,
            connector_id=h.STEP_CONNECTOR_ID,
            op_id=CHANGE_STEP,
            safety_level="safe",
            requires_approval=False,
            target=target,
            params={"name": "thing-1"},
            call=_send_then_time_out,
        )
    except TimeoutError:
        pass
    else:
        if made.stopped is not None:
            return made.stopped
    waited = await _waiting_step(operator, target, CREDENTIAL_STEP, {"path": "app"})
    return waited.stopped if waited.stopped is not None else {"done": True}


async def change_pop_result_wait_wait_composite(
    operator: Operator, target: Any, params: dict[str, Any]
) -> Any:
    """A change whose result the handler changes (``pop``), then two steps that wait."""
    made = await _change_step(operator, target, CHANGE_STEP, {"name": "thing-1"}, {"id": "t-1"})
    if made.stopped is not None:
        return made.stopped
    thing_id = made.value.pop("id")
    first = await _waiting_step(operator, target, CREDENTIAL_STEP, {"path": thing_id})
    if first.stopped is not None:
        return first.stopped
    second = await _waiting_step(operator, target, SECOND_WAIT_STEP, {"id": thing_id})
    if second.stopped is not None:
        return second.stopped
    return {"done": True, "id": thing_id}
