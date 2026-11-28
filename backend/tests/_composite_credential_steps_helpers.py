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

# Declares the class of the Kubernetes reads, so the read that stores a value is a credential read.
import meho_backplane.connectors.kubernetes.op_classes  # noqa: F401
from meho_backplane.auth.operator import Operator
from meho_backplane.operations import DispatchChild, register_typed_operation
from meho_backplane.operations.composite import enforce_subop_policy, run_governed_subop

from . import _composite_wait_audit_helpers as h

#: Every request sent to the target, in order.
sent: list[str] = []

#: A made-up secret value. It sits under neutral field names only.
SECRET_VALUE = "made-up-value-7f3c"

#: What the credential read returns: its version, then its value.
read_answer: dict[str, Any] = {}

#: ``True``: the credential read of ``credential_read_wait_change_composite``
#: is marked ``requires_approval``, so its policy says it must wait.
read_policy: dict[str, bool] = {"waits": False}

CHANGE_STEP = "POST:/v1/sys/things"
SECOND_CHANGE_STEP = "PUT:/v1/sys/things/{id}"
WAIT_STEP = "POST:/v1/sys/things/{id}/seal"
#: Classed ``credential_write``.
CREDENTIAL_WRITE_STEP = "vault.kv.put"
#: Classed ``credential_read``.
CREDENTIAL_READ_STEP = "vault.kv.read"
#: Classed ``credential_write``; run through ``dispatch_child``.
CREDENTIAL_WRITE_CHILD = "vault.kv.patch"
#: Classed ``credential_read``, but it also stores the value it reads.
STORING_READ_STEP = "k8s.secret.read_to_ref"
#: A second step that waits, with its own approval.
SECOND_WAIT_STEP = "POST:/v1/sys/things/{id}/unseal"

WRITE_WAIT_WRITE_OP_ID = "vault.composite.write_wait_write"
CREDENTIAL_WRITE_WAIT_CHANGE_OP_ID = "vault.composite.credential_write_wait_change"
CREDENTIAL_READ_WAIT_CHANGE_OP_ID = "vault.composite.credential_read_wait_change"
APPROVED_READ_WAIT_CHANGE_OP_ID = "vault.composite.approved_credential_read_wait_change"
TWO_CREDENTIAL_WRITES_OP_ID = "vault.composite.two_credential_writes_each_waits"
LOST_CREDENTIAL_WRITE_OP_ID = "vault.composite.credential_write_answer_lost_then_wait"
CHILD_CREDENTIAL_WRITE_OP_ID = "vault.composite.child_credential_write_wait_change"
STORING_READ_OP_ID = "vault.composite.change_storing_read_wait_change"
CHILD_READ_UNDER_RUN_APPROVAL_OP_ID = "vault.composite.wait_child_read_wait_change"
CHILD_READ_WAIT_CHANGE_OP_ID = "vault.composite.child_read_wait_change"
GATED_READ_WAIT_CHANGE_OP_ID = "vault.composite.gated_read_wait_change"


def reset() -> None:
    """Clear the sent requests and set the read answer to its first value."""
    sent.clear()
    read_answer.clear()
    read_answer.update({"version": 1, "value": SECRET_VALUE})
    read_policy["waits"] = False


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
    read = await _step(
        operator,
        target,
        CREDENTIAL_READ_STEP,
        {"path": "app"},
        waits=read_policy["waits"],
        call=_read,
    )
    if read.stopped is not None:
        return read.stopped
    version = {"id": "t-1", "version": read.value["version"]}
    for op_id, waits in ((WAIT_STEP, True), (CHANGE_STEP, False)):
        step = await _step(operator, target, op_id, version, waits=waits)
        if step.stopped is not None:
            return step.stopped
    return {"done": True}


async def _read() -> dict[str, Any]:
    sent.append(CREDENTIAL_READ_STEP)
    return {"data": {"value": read_answer["value"]}, "version": read_answer["version"]}


async def approved_read_wait_change_composite(
    operator: Operator, target: Any, params: dict[str, Any]
) -> Any:
    """A credential read that waits for its own approval, a step that waits, a change."""
    read = await _step(
        operator, target, CREDENTIAL_READ_STEP, {"path": "app"}, waits=True, call=_read
    )
    if read.stopped is not None:
        return read.stopped
    for op_id, waits in ((WAIT_STEP, True), (CHANGE_STEP, False)):
        step = await _step(operator, target, op_id, {"id": "t-1"}, waits=waits)
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
        written = await _step(
            operator,
            target,
            CREDENTIAL_WRITE_STEP,
            _credential_write_params("app"),
            waits=False,
            call=_send_then_time_out,
        )
        if written.stopped is not None:
            return written.stopped
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


async def change_storing_read_wait_change_composite(
    operator: Operator, target: Any, params: dict[str, Any]
) -> Any:
    """A change, a credential read that also stores a value (no approval), a wait, a change."""

    async def _read_and_store() -> dict[str, Any]:
        sent.append(STORING_READ_STEP)
        return {"ref": "kv/app#value"}

    plan: tuple[tuple[str, dict[str, Any], bool, Callable[[], Awaitable[Any]] | None], ...] = (
        (CHANGE_STEP, {"id": "t-1"}, False, None),
        (STORING_READ_STEP, {"name": "app", "key": "value"}, False, _read_and_store),
        (WAIT_STEP, {"id": "t-1"}, True, None),
        (SECOND_CHANGE_STEP, {"id": "t-1"}, False, None),
    )
    for op_id, step_params, waits, call in plan:
        step = await _step(operator, target, op_id, step_params, waits=waits, call=call)
        if step.stopped is not None:
            return step.stopped
    return {"done": True}


async def credential_read_child_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Typed credential read run through ``dispatch_child``."""
    sent.append(CREDENTIAL_READ_STEP)
    return {"data": {"value": SECRET_VALUE}, "version": 1}


async def register_credential_read_child(
    embedding: Any, safety_level: str = "dangerous", *, requires_approval: bool = False
) -> None:
    """Register the credential read run through ``dispatch_child``.

    ``dangerous``: a person's call waits, so only the approval of the run lets it go ahead.
    ``safe``: it goes ahead with no person. ``requires_approval``: every call waits.
    """
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=CREDENTIAL_READ_STEP,
        handler=credential_read_child_handler,
        summary="Read a secret.",
        description="Read a secret.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        safety_level=safety_level,
        requires_approval=requires_approval,
        embedding_service=embedding,
    )


async def child_read_wait_change_composite(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: DispatchChild
) -> Any:
    """A credential read through ``dispatch_child``, a wait, a change."""
    read = await dispatch_child(
        connector_id=h.CONNECTOR_ID, op_id=CREDENTIAL_READ_STEP, params={"path": "app"}
    )
    if read.status != "ok":
        return read
    for op_id, waits in ((WAIT_STEP, True), (CHANGE_STEP, False)):
        step = await _step(operator, target, op_id, {"id": "t-1"}, waits=waits)
        if step.stopped is not None:
            return step.stopped
    return {"done": True}


async def wait_child_read_wait_change_composite(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: DispatchChild
) -> Any:
    """A wait, a credential read through ``dispatch_child``, a second wait, a change."""
    first = await _step(operator, target, WAIT_STEP, {"id": "t-1"}, waits=True)
    if first.stopped is not None:
        return first.stopped
    read = await dispatch_child(
        connector_id=h.CONNECTOR_ID, op_id=CREDENTIAL_READ_STEP, params={"path": "app"}
    )
    if read.status != "ok":
        return read
    for op_id, waits in ((SECOND_WAIT_STEP, True), (CHANGE_STEP, False)):
        step = await _step(operator, target, op_id, {"id": "t-1"}, waits=waits)
        if step.stopped is not None:
            return step.stopped
    return {"done": True}


async def gated_read_wait_change_composite(
    operator: Operator, target: Any, params: dict[str, Any]
) -> Any:
    """A credential read checked by ``enforce_subop_policy``, a step that waits, a change."""
    gated = await enforce_subop_policy(
        operator=operator,
        connector_id=h.STEP_CONNECTOR_ID,
        op_id=CREDENTIAL_READ_STEP,
        safety_level="safe",
        requires_approval=False,
        target=target,
        params={"path": "app"},
    )
    if gated is not None:
        return gated
    read = await _read()
    version = {"id": "t-1", "version": read["version"]}
    for op_id, waits in ((WAIT_STEP, True), (CHANGE_STEP, False)):
        step = await _step(operator, target, op_id, version, waits=waits)
        if step.stopped is not None:
            return step.stopped
    return {"done": True}
