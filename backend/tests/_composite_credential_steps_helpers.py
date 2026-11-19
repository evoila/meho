# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Multi-step operations with a credential step and a later wait.

Used by ``test_operations_composite_credential_steps_resume.py``.

Each operation has a step that reads or writes a credential and a later step
that waits for a person. Every request a step sends is added to :data:`sent`,
so a test can count the requests that reached the target. The dispatcher
imports these handlers by their module path (``handler_ref``); closures are
rejected.

Connector, operator and registration come from
``_composite_wait_audit_helpers``.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

from meho_backplane.auth.operator import Operator
from meho_backplane.operations import DispatchChild, register_typed_operation
from meho_backplane.operations.composite import run_governed_subop

from . import _composite_wait_audit_helpers as h

#: Every request sent to the target, in order.
sent: list[str] = []

#: A made-up secret value. It sits under neutral field names only.
SECRET_VALUE = "made-up-value-7f3c"

#: What the credential read returns: its version, then its value.
read_answer: dict[str, Any] = {}

CHANGE_STEP = "POST:/v1/sys/things"
SECOND_CHANGE_STEP = "PUT:/v1/sys/things/{id}"
WAIT_STEP = "POST:/v1/sys/things/{id}/seal"
#: Classed ``credential_write``.
CREDENTIAL_WRITE_STEP = "vault.kv.put"
#: Classed ``credential_read``.
CREDENTIAL_READ_STEP = "vault.kv.read"
#: Classed ``credential_write``; run through ``dispatch_child``.
CREDENTIAL_WRITE_CHILD = "vault.kv.patch"

WRITE_WAIT_WRITE_OP_ID = "vault.composite.write_wait_write"
CREDENTIAL_WRITE_WAIT_CHANGE_OP_ID = "vault.composite.credential_write_wait_change"
CREDENTIAL_READ_WAIT_CHANGE_OP_ID = "vault.composite.credential_read_wait_change"
TWO_CREDENTIAL_WRITES_OP_ID = "vault.composite.two_credential_writes_each_waits"
LOST_CREDENTIAL_WRITE_OP_ID = "vault.composite.credential_write_answer_lost_then_wait"
CHILD_CREDENTIAL_WRITE_OP_ID = "vault.composite.child_credential_write_wait_change"


def reset() -> None:
    """Clear the sent requests and set the read answer to its first value."""
    sent.clear()
    read_answer.clear()
    read_answer.update({"version": 1, "value": SECRET_VALUE})


async def _step(
    operator: Operator,
    target: Any,
    op_id: str,
    params: dict[str, Any],
    *,
    waits: bool,
    call: Callable[[], Awaitable[Any]] | None = None,
) -> Any:
    """One direct step through ``run_governed_subop``; records itself when it runs."""

    async def _send() -> dict[str, Any]:
        sent.append(op_id)
        return {"ok": True}

    return await run_governed_subop(
        operator=operator,
        connector_id=h.STEP_CONNECTOR_ID,
        op_id=op_id,
        safety_level="safe",
        requires_approval=waits,
        target=target,
        params=params,
        call=call or _send,
    )


def _credential_write_call() -> Callable[[], Awaitable[Any]]:
    async def _send() -> dict[str, Any]:
        sent.append(CREDENTIAL_WRITE_STEP)
        return {"data": {"value": SECRET_VALUE}, "version": 3}

    return _send


def _credential_write_params(path: str) -> dict[str, Any]:
    return {"path": path, "data": {"value": SECRET_VALUE}}


async def write_wait_write_composite(
    operator: Operator, target: Any, params: dict[str, Any]
) -> Any:
    """A change, a step that waits, a change."""
    for op_id, waits in ((CHANGE_STEP, False), (WAIT_STEP, True), (SECOND_CHANGE_STEP, False)):
        step = await _step(operator, target, op_id, {"id": "t-1"}, waits=waits)
        if step.stopped is not None:
            return step.stopped
    return {"done": True}


async def credential_write_wait_change_composite(
    operator: Operator, target: Any, params: dict[str, Any]
) -> Any:
    """A credential write, a step that waits, a change."""
    written = await _step(
        operator,
        target,
        CREDENTIAL_WRITE_STEP,
        _credential_write_params("app"),
        waits=False,
        call=_credential_write_call(),
    )
    if written.stopped is not None:
        return written.stopped
    for op_id, waits in ((WAIT_STEP, True), (CHANGE_STEP, False)):
        step = await _step(operator, target, op_id, {"id": "t-1"}, waits=waits)
        if step.stopped is not None:
            return step.stopped
    return {"done": True, "credential_replayed": written.replayed}


async def credential_read_wait_change_composite(
    operator: Operator, target: Any, params: dict[str, Any]
) -> Any:
    """A credential read, a step that waits with the read's version, a change."""

    async def _read() -> dict[str, Any]:
        sent.append(CREDENTIAL_READ_STEP)
        return {"data": {"value": read_answer["value"]}, "version": read_answer["version"]}

    read = await _step(
        operator, target, CREDENTIAL_READ_STEP, {"path": "app"}, waits=False, call=_read
    )
    if read.stopped is not None:
        return read.stopped
    version = {"id": "t-1", "version": read.value["version"]}
    for op_id, waits in ((WAIT_STEP, True), (CHANGE_STEP, False)):
        step = await _step(operator, target, op_id, version, waits=waits)
        if step.stopped is not None:
            return step.stopped
    return {"done": True}


async def two_credential_writes_composite(
    operator: Operator, target: Any, params: dict[str, Any]
) -> Any:
    """Two credential writes; each one waits for a person."""
    for path in ("app-a", "app-b"):
        step = await _step(
            operator,
            target,
            CREDENTIAL_WRITE_STEP,
            _credential_write_params(path),
            waits=True,
            call=_credential_write_call(),
        )
        if step.stopped is not None:
            return step.stopped
    return {"done": True}


async def credential_write_answer_lost_composite(
    operator: Operator, target: Any, params: dict[str, Any]
) -> Any:
    """A credential write whose answer is lost after it was sent, then a step that waits."""

    async def _send_then_time_out() -> dict[str, Any]:
        sent.append(CREDENTIAL_WRITE_STEP)
        raise TimeoutError("no answer from the target")

    with contextlib.suppress(TimeoutError):
        await _step(
            operator,
            target,
            CREDENTIAL_WRITE_STEP,
            _credential_write_params("app"),
            waits=False,
            call=_send_then_time_out,
        )
    waited = await _step(operator, target, WAIT_STEP, {"id": "t-1"}, waits=True)
    return waited.stopped if waited.stopped is not None else {"done": True}


async def credential_write_child_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Typed credential write run through ``dispatch_child``."""
    sent.append(CREDENTIAL_WRITE_CHILD)
    return {"data": {"value": SECRET_VALUE}, "version": 4}


async def register_credential_write_child(embedding: Any) -> None:
    """Register the typed credential write run through ``dispatch_child``."""
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=CREDENTIAL_WRITE_CHILD,
        handler=credential_write_child_handler,
        summary="Patch a secret.",
        description="Patch a secret.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        safety_level="safe",
        embedding_service=embedding,
    )


async def child_credential_write_wait_change_composite(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: DispatchChild
) -> Any:
    """A credential write through ``dispatch_child``, a step that waits, a change."""
    written = await dispatch_child(
        connector_id=h.CONNECTOR_ID,
        op_id=CREDENTIAL_WRITE_CHILD,
        params=_credential_write_params("app"),
    )
    if written.status != "ok":
        return written
    for op_id, waits in ((WAIT_STEP, True), (CHANGE_STEP, False)):
        step = await _step(operator, target, op_id, {"id": "t-1"}, waits=waits)
        if step.stopped is not None:
            return step.stopped
    return {"done": True, "child_result": written.result}
