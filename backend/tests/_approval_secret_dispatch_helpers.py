# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Handlers for ``test_approval_secret_values_dispatch.py``.

The dispatcher imports a handler by its module path, so the handlers live
at module level here. Each one records the params it got, so a test can
check what the approved run sent.
"""

from __future__ import annotations

from typing import Any

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.operations import register_typed_operation
from meho_backplane.operations.composite import enforce_subop_policy

from . import _composite_wait_audit_helpers as h

NOTE_OP_ID = "vault.things.note_update"
PARENT_OP_ID = "vault.composite.parent_with_secret"

#: The params each handler got, in order.
received: list[tuple[str, dict[str, Any]]] = []


async def note_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Save a note; send the saved note and the PIN back, as some APIs do."""
    received.append((NOTE_OP_ID, dict(params)))
    return {"saved": {"note": params.get("note"), "pin": params.get("password")}}


async def register_note_op(embedding: Any) -> None:
    """Register the typed note write: every call waits for a person."""
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=NOTE_OP_ID,
        handler=note_handler,
        summary="Update a note.",
        description="Update a note.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        safety_level="caution",
        requires_approval=True,
        embedding_service=embedding,
    )


async def parent_with_secret_composite(
    operator: Operator, target: Any, params: dict[str, Any]
) -> Any:
    """A multi-step operation that takes a secret; its one step takes only an id."""
    received.append((PARENT_OP_ID, dict(params)))
    gated = await h.gate(operator, target, h.STEP_A)
    if gated is not None:
        return gated
    return {"done": True}


ECHO_OP_ID = "vault.things.note_echo"
REJECT_OP_ID = "vault.things.note_reject"
STEP_NOTE_PARENT_OP_ID = "vault.composite.step_with_note"
#: The note the multi-step operation computes for its step.
COMPUTED_NOTE = "computed-step-note-9137"


async def echo_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Save a note and keys; send them back inside longer text and under other names."""
    received.append((ECHO_OP_ID, dict(params)))
    return {"message": f"Saved note: {params.get('note')}", "installed": params.get("licenseKeys")}


async def reject_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Refuse the note with an HTTP 422 whose body repeats it."""
    received.append((REJECT_OP_ID, dict(params)))
    request = httpx.Request("POST", "https://vault.test/v1/notes")
    response = httpx.Response(
        422, json={"message": f"Note refused: {params.get('note')}"}, request=request
    )
    raise httpx.HTTPStatusError("422 Unprocessable Entity", request=request, response=response)


async def register_echo_ops(embedding: Any) -> None:
    """Register the two typed note writes above: every call waits for a person."""
    for op_id, handler in ((ECHO_OP_ID, echo_handler), (REJECT_OP_ID, reject_handler)):
        await register_typed_operation(
            product="vault",
            version="1.x",
            impl_id="vault",
            op_id=op_id,
            handler=handler,
            summary="Update a note.",
            description="Update a note.",
            parameter_schema={"type": "object"},
            when_to_use=None,
            safety_level="caution",
            requires_approval=True,
            embedding_service=embedding,
        )


async def step_with_note_composite(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    """A multi-step operation that computes a note for its step and sends it back."""
    received.append((STEP_NOTE_PARENT_OP_ID, dict(params)))
    gated = await enforce_subop_policy(
        operator=operator,
        connector_id=h.STEP_CONNECTOR_ID,
        op_id=h.STEP_A,
        safety_level="safe",
        requires_approval=True,
        target=target,
        params={"name": "vm-1", "note": COMPUTED_NOTE},
    )
    if gated is not None:
        return gated
    return {"done": True, "message": f"Saved note: {COMPUTED_NOTE}"}
