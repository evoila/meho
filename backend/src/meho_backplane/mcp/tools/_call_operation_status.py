# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An MCP caller reads the state of its own approval request (``MCP_OWN_REQUEST_STATUS``).

A call that waits for approval answers ``awaiting_approval`` with an
``extras.approval_request_id``. On the working surface there was no way to
follow that request. This module adds one:

``call_operation(connector_id, op_id, approval_request_id)``

* Only with the switch ``MCP_OWN_REQUEST_STATUS`` on (off by default). With
  it off, :func:`status_for_settings` leaves the input and its text out of
  ``call_operation``, so the input check refuses it.
* With ``MCP_REVEAL_FETCH`` also on, the handler first looks up the request
  to see if it is the caller's own approved secret reveal
  (:func:`is_approved_reveal`). That lookup only picks the path: it writes
  no audit row and its result is not sent. A reveal goes to the fetch. Every
  other request comes here, and the checks below begin.
* Only ``connector_id``, ``op_id`` and ``approval_request_id`` may be sent.
  Any other input is refused as bad input (``-32602``). This is the first
  check of this module, so the request is not read for the answer and no
  audit row is written.
* The call runs nothing and makes no new request. It only reads.
* The checks run in this order. Each refusal writes one audit row (path
  ``approval.status``, with an ``outcome``) and no ``approval.result`` row:

  1. the request is the caller's own, in its tenant. Another person, another
     tenant and an unknown id all get the same answer,
     ``approval_request_not_found``. The audit row of that refusal is the
     caller's own and is not linked to any request;
  2. under an agent's connector list, the request's connector and target are
     inside the list, else ``connector_not_allowed``;
  3. ``connector_id`` and ``op_id`` are the ones of the request, else
     ``request_wrong_operation``. For a request made by a step of a
     multi-step operation, they are the step's, not the operation's.

* A read that passes the checks writes one ``approval.result`` audit row, as
  ``GET /api/v1/approvals/{id}/result`` does.
* An approved secret reveal with ``MCP_REVEAL_FETCH`` on still goes to the
  fetch of :mod:`._call_operation_reveal`, as before.

The answer by state:

* waiting: the same ``awaiting_approval`` answer the first call got, with its
  ``audit_id`` and its note about a change of rights;
* approved, not run yet or running: ``status="pending"`` with
  ``extras.result_state`` of ``not_started`` or ``started``;
* approved and run: the stored result of the run (the same envelope the REST
  read gives), with ``extras.result_state`` and
  ``extras.approval_request_id`` added. ``result_state`` is ``executed``,
  ``failed`` or ``uncertain``: with ``uncertain`` MEHO does not know whether
  the change happened, and it never runs it again. A stored result that is
  not an operation result gives ``status="error"`` with
  ``extras.error_code="approval_result_unreadable"``,
  ``extras.result_state="uncertain"`` and ``extras.retry=False``: the
  operation may have run, so the call must not be sent again. The read
  writes one more audit row.
  An ``ok`` answer over the response
  size limit is trimmed by ``bound_complete_response``: the status and the
  ``audit_id`` stay, and ``delivery`` says what was left out;
* rejected, expired or withdrawn: ``status="denied"`` with an
  ``extras.error_code`` that names the state.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from pydantic import ValidationError

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus, AuditEventClass
from meho_backplane.mcp.audit import write_mcp_audit_row
from meho_backplane.mcp.registry import ToolDefinition
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.mcp.tools._call_operation_reveal import APPROVAL_REQUEST_ID
from meho_backplane.operations._errors import result_awaiting_approval
from meho_backplane.operations._target_connector_limit import stored_request_outside_limit
from meho_backplane.operations.approval_queue import (
    EXPIRED_REASON_MESSAGES,
    ApprovalExpiredReason,
    ApprovalNotFoundError,
    ApprovalResultState,
    ResultAccessForbiddenError,
    _write_audit_row,
    approval_expired_reason,
    approval_result_state,
    get_request,
    read_approval_result,
    rights_change_note,
)
from meho_backplane.operations.approved_reveal import is_secret_reveal
from meho_backplane.operations.delivery_budget import bound_complete_response
from meho_backplane.operations.deny_reasons import CONNECTOR_NOT_ALLOWED
from meho_backplane.settings import get_settings
from meho_backplane.untrusted_text import with_data_notice

__all__ = [
    "is_approved_reveal",
    "read_own_request_over_mcp",
    "status_for_settings",
]

#: The path of the audit row of a refused read.
_REFUSAL_PATH: Final[str] = "approval.status"
_NOT_FOUND_STATUS_CODE: Final[int] = 404
_REFUSED_STATUS_CODE: Final[int] = 409
_UNREADABLE_STATUS_CODE: Final[int] = 422

_NOT_FOUND: Final[str] = "approval_request_not_found"
_WRONG_OPERATION: Final[str] = "request_wrong_operation"
_RESULT_UNREADABLE: Final[str] = "approval_result_unreadable"

_UNREADABLE_MESSAGE: Final[str] = (
    "The request was approved and the operation may have run. MEHO cannot read "
    "its stored result. Do not send this call again. Ask a person to look at the "
    "request in the console."
)

#: The only inputs a status read may send.
_STATUS_INPUTS: Final[frozenset[str]] = frozenset({"connector_id", "op_id", APPROVAL_REQUEST_ID})

#: The one answer for a request that is not the caller's, not in the tenant, or unknown.
_NOT_FOUND_MESSAGE: Final[str] = "You have no approval request with this id."

_ASK_AGAIN: Final[str] = "Ask again if the change is still needed."

_DENIED_MESSAGES: Final[dict[str, str]] = {
    ApprovalRequestStatus.REJECTED.value: (
        f"A person rejected the request. Nothing ran. {_ASK_AGAIN}"
    ),
    ApprovalRequestStatus.WITHDRAWN.value: (
        f"The request was taken back. Nothing ran. {_ASK_AGAIN}"
    ),
}

_PROPERTY_TEXT: Final[str] = (
    "To follow your own approval request: its `extras.approval_request_id`. "
    "Send only `connector_id`, `op_id` and this. MEHO runs nothing and makes "
    "no new request. It answers with the state of the request."
)

_TOOL_TEXT: Final[str] = (
    "\n\nA call that waits for approval gives `extras.approval_request_id`. To "
    "see the state of that request, call again with the same `connector_id` and "
    "`op_id` and `approval_request_id`. Nothing runs. This works only for your "
    "own requests."
)


def status_for_settings(definition: ToolDefinition) -> ToolDefinition:
    """Return ``call_operation`` with the status input and its text while the switch is on.

    With ``MCP_OWN_REQUEST_STATUS`` off, *definition* comes back unchanged.
    """
    if not get_settings().mcp_own_request_status:
        return definition
    schema = definition.inputSchema
    properties = dict(schema.get("properties", {}))
    existing = properties.get(APPROVAL_REQUEST_ID)
    text = _PROPERTY_TEXT
    if existing is not None:
        text = f"{_PROPERTY_TEXT} {existing['description']}"
    properties[APPROVAL_REQUEST_ID] = {"type": "string", "format": "uuid", "description": text}
    return definition.model_copy(
        update={
            "description": definition.description + _TOOL_TEXT,
            "inputSchema": {**schema, "properties": properties},
        }
    )


class _UnreadableResultError(Exception):
    """The stored result of an approved request is not an operation result."""


def _denied(op_id: str, request_id: uuid.UUID, error_code: str, message: str) -> dict[str, Any]:
    """An answer with no state of a request: nothing ran, nothing was read."""
    return with_data_notice(
        OperationResult(
            status="denied",
            op_id=op_id,
            error=f"{error_code}: {message}",
            duration_ms=0.0,
            extras={"error_code": error_code, "approval_request_id": str(request_id)},
        ).model_dump(mode="json")
    )


def _unreadable(op_id: str, request_id: uuid.UUID) -> dict[str, Any]:
    """The answer for an approved request whose stored result cannot be read.

    The operation may have run, so this is ``error`` with an ``uncertain``
    result, not ``denied``: ``denied`` means that nothing ran.
    """
    return with_data_notice(
        OperationResult(
            status="error",
            op_id=op_id,
            error=f"{_RESULT_UNREADABLE}: {_UNREADABLE_MESSAGE}",
            duration_ms=0.0,
            extras={
                "error_code": _RESULT_UNREADABLE,
                "approval_request_id": str(request_id),
                "approval_status": ApprovalRequestStatus.APPROVED.value,
                "result_state": ApprovalResultState.UNCERTAIN.value,
                "retry": False,
            },
        ).model_dump(mode="json")
    )


def _parse_request_id(raw: Any) -> uuid.UUID:
    try:
        return uuid.UUID(str(raw))
    except ValueError as exc:
        raise McpInvalidParamsError(
            "`approval_request_id` must be a UUID",
            data={"reason": "approval_request_id_not_uuid"},
        ) from exc


async def _owned_request(operator: Operator, request_id: uuid.UUID) -> ApprovalRequest | None:
    """The request when the caller owns it in its tenant, else ``None``. Writes no audit row."""
    try:
        async with get_sessionmaker()() as session:
            row = await get_request(session, tenant_id=operator.tenant_id, request_id=request_id)
    except ApprovalNotFoundError:
        return None
    return row if row.principal_sub == operator.sub else None


async def is_approved_reveal(operator: Operator, arguments: dict[str, Any]) -> bool:
    """True when ``approval_request_id`` names the caller's own approved secret reveal.

    Such a request goes to the value fetch while ``MCP_REVEAL_FETCH`` is on.
    A bad id, an unknown request, and a request of another person or tenant
    give ``False``, so they get the plain "not found" answer.
    """
    try:
        request_id = uuid.UUID(str(arguments[APPROVAL_REQUEST_ID]))
    except ValueError:
        return False
    row = await _owned_request(operator, request_id)
    return (
        row is not None
        and is_secret_reveal(row)
        and row.status == ApprovalRequestStatus.APPROVED.value
    )


async def _audit_refusal(
    operator: Operator, request: ApprovalRequest, outcome: str, status_code: int
) -> None:
    """Write the audit row of a refused read of *request*, linked to it.

    The row names the session of this read, not the session that made the
    request. Its parent is still the request's ``approval.request`` row.
    """
    async with get_sessionmaker()() as session:
        await _write_audit_row(
            session,
            audit_id=uuid.uuid4(),
            event_class=AuditEventClass.POLICY_DECISION,
            operator=operator,
            request=request,
            path=_REFUSAL_PATH,
            status_code=status_code,
            duration_ms=0.0,
            extra_payload={"outcome": outcome},
            caller_session=True,
        )
        await session.commit()


async def _not_found(operator: Operator, op_id: str, request_id: uuid.UUID) -> dict[str, Any]:
    """The one "not found" answer, with one audit row of the caller's own.

    No request row exists for the caller, so the row is linked to none: it
    is in the caller's tenant, names the caller, and holds only the outcome
    and the id the caller asked for. A request of another person or tenant
    gets nothing in its own history.
    """
    await write_mcp_audit_row(
        event_class=AuditEventClass.POLICY_DECISION,
        operator=operator,
        method="APPROVAL",
        path=_REFUSAL_PATH,
        status_code=_NOT_FOUND_STATUS_CODE,
        duration_ms=0.0,
        payload={"outcome": _NOT_FOUND, "asked_request_id": str(request_id)},
    )
    return _denied(op_id, request_id, _NOT_FOUND, _NOT_FOUND_MESSAGE)


async def _answer_for(row: ApprovalRequest) -> dict[str, Any]:
    """The answer for the state of *row*."""
    request_id = row.id
    if row.status == ApprovalRequestStatus.PENDING.value:
        # The same answer as the first call: its audit row and its note.
        return with_data_notice(
            result_awaiting_approval(
                row.op_id,
                request_id,
                0.0,
                audit_id=row.request_audit_id,
                note=await rights_change_note(row),
            ).model_dump(mode="json")
        )
    if row.status == ApprovalRequestStatus.APPROVED.value:
        return _approved_answer(row)
    if row.status == ApprovalRequestStatus.EXPIRED.value:
        reason = approval_expired_reason(row) or ApprovalExpiredReason.DEADLINE_PASSED
        message = EXPIRED_REASON_MESSAGES[reason]
    else:
        message = _DENIED_MESSAGES[row.status]
    return _denied(row.op_id, request_id, f"approval_request_{row.status}", message)


def _stored_result_answer(envelope: dict[str, Any], extras: dict[str, Any]) -> dict[str, Any]:
    """The stored result with the approval fields added, inside the response size limit.

    The added fields make the stored result larger, so the limit is checked
    on the answer that is sent. The limiter also counts ``data_notice``.
    The stored result is checked as it is stored, before the fields are
    added, so a wrong field in it is never hidden by the added ones.
    """
    try:
        stored = OperationResult.model_validate(envelope)
    except ValidationError as exc:
        raise _UnreadableResultError from exc
    stored = OperationResult.model_validate({**envelope, "extras": {**stored.extras, **extras}})
    bounded = bound_complete_response(
        stored, get_settings().result_complete_response_wire_max_bytes
    )
    return with_data_notice(bounded.model_dump(mode="json"))


def _approved_answer(row: ApprovalRequest) -> dict[str, Any]:
    if row.resume_result is not None and not isinstance(row.resume_result, dict):
        raise _UnreadableResultError
    state = approval_result_state(row)
    assert state is not None  # the row is approved
    extras: dict[str, Any] = {
        "approval_request_id": str(row.id),
        "approval_status": ApprovalRequestStatus.APPROVED.value,
        "result_state": state.value,
    }
    envelope = row.resume_result
    if envelope is not None:
        return _stored_result_answer(envelope, extras)
    if is_secret_reveal(row):
        next_step = (
            "Approved. The operation did not run. It runs once, when the person "
            "who asked gets the value."
        )
    else:
        next_step = "Approved. The operation is running. Call again later to read the result."
    return with_data_notice(
        OperationResult(
            status="pending",
            op_id=row.op_id,
            error=None,
            duration_ms=0.0,
            extras={**extras, "next_step": next_step},
        ).model_dump(mode="json")
    )


async def read_own_request_over_mcp(
    operator: Operator, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Answer with the state of the caller's own approval request. Nothing runs."""
    if not get_settings().mcp_own_request_status:
        raise McpInvalidParamsError(
            "`approval_request_id` is not an input of call_operation here",
            data={"reason": "own_request_status_off"},
        )
    extra = sorted(set(arguments) - _STATUS_INPUTS)
    if extra:
        raise McpInvalidParamsError(
            "with `approval_request_id` send only `connector_id` and `op_id`; "
            f"remove: {', '.join(extra)}",
            data={"reason": "own_request_status_extra_inputs", "inputs": extra},
        )
    request_id = _parse_request_id(arguments[APPROVAL_REQUEST_ID])
    connector_id = str(arguments.get("connector_id", ""))
    op_id = str(arguments.get("op_id", ""))
    owned = await _owned_request(operator, request_id)
    if owned is None:
        return await _not_found(operator, op_id, request_id)
    # Before the wrong-operation check: that answer names the request's
    # connector, and a refusal under a list names no connector outside it.
    if await stored_request_outside_limit(operator, owned):
        await _audit_refusal(operator, owned, CONNECTOR_NOT_ALLOWED, _REFUSED_STATUS_CODE)
        return _denied(
            op_id,
            request_id,
            CONNECTOR_NOT_ALLOWED,
            "The request is not for a target of this agent's allowed connectors.",
        )
    if owned.connector_id != connector_id or owned.op_id != op_id:
        await _audit_refusal(operator, owned, _WRONG_OPERATION, _REFUSED_STATUS_CODE)
        return _denied(
            op_id,
            request_id,
            _WRONG_OPERATION,
            f"The request is for `{owned.op_id}` on `{owned.connector_id}`. Send those.",
        )
    try:
        async with get_sessionmaker()() as session:
            row = await read_approval_result(session, operator=operator, request_id=request_id)
            await session.commit()
    except (ApprovalNotFoundError, ResultAccessForbiddenError):
        return await _not_found(operator, op_id, request_id)
    try:
        return await _answer_for(row)
    except _UnreadableResultError:
        await _audit_refusal(operator, row, _RESULT_UNREADABLE, _UNREADABLE_STATUS_CODE)
        return _unreadable(row.op_id, request_id)
