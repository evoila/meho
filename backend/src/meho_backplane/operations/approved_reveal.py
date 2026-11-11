# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The person who asked gets the value of an approved secret reveal.

A request with ``reveal_secret: true`` that waits for a second person's
approval follows one rule, for a read
(:mod:`meho_backplane.operations.credential_read`) and for an operation that
creates a credential (for example a token). The approval does not run the
operation. It is a one-time permission for the
person who asked:

* Approving runs nothing. Every approve surface goes through
  :func:`~meho_backplane.operations.approval_queue.resume_dispatch_after_approval`,
  which answers with :func:`result_waiting_for_requester` for such a request.
  So the answer to the approving person holds no secret value.
* The operation runs when the person who asked fetches the value
  (:func:`fetch_approved_reveal`): ``POST /api/v1/approvals/{id}/reveal``,
  the "Show value" button of the Vault console, or
  ``meho secret read --approval <id>``. The value goes only to that person,
  in that one answer.
* The fetch works once, and only within
  ``APPROVAL_REVEAL_FETCH_WINDOW_SECONDS`` (15 minutes by default) after the
  approval. The one-resumer claim makes the second fetch fail.
* The fetch is tied to the request: same person, same tenant, same
  operation and target, and the stored params must still match the stored
  hash.
* MEHO keeps no value. The run result stored on the request
  (``resume_result``) is the answer without its values
  (:func:`without_secret_values`). The audit row of the run stores
  fingerprints only, as for every reveal.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.db.models import ApprovalRequest

__all__ = [
    "WAITING_FOR_REQUESTER",
    "RevealFetchRefusedError",
    "fetch_approved_reveal",
    "is_secret_reveal",
    "is_write_reveal",
    "result_waiting_for_requester",
    "reveal_fetch_window",
    "without_secret_values",
]

#: Status of the answer to an approve of a reveal: nothing ran, the person
#: who asked can now fetch the value.
WAITING_FOR_REQUESTER: Final[str] = "waiting_for_requester"

#: The op classes (:func:`~meho_backplane.broadcast.events.classify_op`) of an
#: operation that only reads. Every other class counts as a change.
_READ_CLASSES: Final[frozenset[str]] = frozenset({"read", "credential_read"})

#: Status code of the ``approval.reveal`` audit row of a refused fetch.
_REFUSED_STATUS_CODE: Final[int] = 409
_FETCHED_STATUS_CODE: Final[int] = 200


class RevealFetchRefusedError(Exception):
    """The fetch of an approved reveal is refused; nothing ran.

    *code* is a short, stable reason (``not_a_reveal``, ``not_approved``,
    ``already_used``, ``window_passed``, ``params_changed``). *message* says
    in plain words what happened and what to do.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def is_secret_reveal(request: ApprovalRequest) -> bool:
    """True when the caller of *request* wants the hidden values of the answer.

    This holds for every request with ``reveal_secret``: a read, and an
    operation that creates a credential. Approving such a request runs
    nothing; it runs once, when the person who asked fetches the value.
    """
    return bool(request.reveal_secret)


def is_write_reveal(request: ApprovalRequest) -> bool:
    """True for a secret reveal that changes something (for example it creates a token).

    Such a request has not changed anything after the approve. An approve
    in order stops at it, so the steps after it do not run before it.

    Only an operation of class ``read`` or ``credential_read`` counts as a
    read. Every other class counts as a change, also ``other`` (for example
    an apply, a scale, or a reboot). When in doubt, the approve in order stops.
    """
    return is_secret_reveal(request) and classify_op(request.op_id) not in _READ_CLASSES


def reveal_fetch_window() -> timedelta:
    """How long after the approval the person who asked may fetch the value."""
    return timedelta(seconds=get_settings().approval_reveal_fetch_window_seconds)


def result_waiting_for_requester(request: ApprovalRequest) -> OperationResult:
    """The answer to an approve of a reveal: approved, nothing ran, no value."""
    minutes = max(1, int(reveal_fetch_window().total_seconds()) // 60)
    return OperationResult(
        status=WAITING_FOR_REQUESTER,
        op_id=request.op_id,
        error=None,
        duration_ms=0.0,
        extras={
            "result_status": WAITING_FOR_REQUESTER,
            "approval_request_id": str(request.id),
            "next_step": (
                f"Approved. The operation did not run. It runs once, when the person "
                f"who asked gets the value, within {minutes} minutes: "
                f"`POST /api/v1/approvals/{request.id}/reveal`, "
                f"`meho secret read --approval {request.id}`, or the 'Show value' "
                f"button in the console."
            ),
        },
    )


def without_secret_values(result: Any) -> Any:
    """*result* without its answer, for the copy MEHO stores on the request.

    The status, the error, the audit id and the other fields stay, so every
    view still shows how the run ended. The answer and its handle go.
    """
    if not isinstance(result, OperationResult):
        return result
    return result.model_copy(
        update={
            "result": None,
            "handle": None,
            "extras": {**dict(result.extras), "secret_value_withheld": True},
        }
    )


def _check_fetch(request: ApprovalRequest, *, now: datetime) -> None:
    """Raise :class:`RevealFetchRefusedError` when *request* may not be fetched now."""
    from meho_backplane.db.models import ApprovalRequestStatus
    from meho_backplane.operations._validate import params_hash_matches

    if not is_secret_reveal(request):
        raise RevealFetchRefusedError(
            "not_a_reveal", "This request does not ask to show a secret value."
        )
    if request.status != ApprovalRequestStatus.APPROVED.value:
        raise RevealFetchRefusedError(
            "not_approved",
            f"The request is {request.status}. The value can be fetched only after the approval.",
        )
    if request.resumed_at is not None:
        raise RevealFetchRefusedError(
            "already_used",
            "The value of this request was already fetched. Send the request again.",
        )
    decided_at = request.decided_at
    if decided_at is not None and decided_at.tzinfo is None:
        decided_at = decided_at.replace(tzinfo=UTC)
    if decided_at is None or now > decided_at + reveal_fetch_window():
        raise RevealFetchRefusedError(
            "window_passed",
            "The time to fetch the value has passed. Send the request again.",
        )
    stored = request.params
    if request.execution_handle is None and (
        not isinstance(stored, dict) or not params_hash_matches(stored, request.params_hash)
    ):
        raise RevealFetchRefusedError(
            "params_changed",
            "The stored request no longer matches the approved one. Send the request again.",
        )


async def fetch_approved_reveal(
    operator: Operator,
    request_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> OperationResult:
    """Run the approved reveal *request_id* once for the person who asked.

    Raises:
        ApprovalNotFoundError: No request with this id in the caller's tenant.
        ResultAccessForbiddenError: The caller did not send the request.
        RevealFetchRefusedError: The fetch is not allowed (see
            :func:`_check_fetch`), or another fetch won the run. Nothing ran.

    Every attempt by the person who asked writes one ``approval.reveal``
    audit row (class ``approval_decision``: it uses up the approval) with
    the outcome. The run itself writes its own audit row, with
    fingerprints only.
    """
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import AuditEventClass
    from meho_backplane.operations.approval_queue import (
        ResultAccessForbiddenError,
        _write_audit_row,
        get_request,
        resume_dispatch_after_approval,
    )

    async with get_sessionmaker()() as session:
        request = await get_request(session, tenant_id=operator.tenant_id, request_id=request_id)
        if operator.sub != request.principal_sub:
            raise ResultAccessForbiddenError(request_id=request_id, principal_sub=operator.sub)
        refusal: RevealFetchRefusedError | None = None
        try:
            _check_fetch(request, now=now or datetime.now(UTC))
        except RevealFetchRefusedError as exc:
            refusal = exc
        await _write_audit_row(
            session,
            audit_id=uuid.uuid4(),
            event_class=AuditEventClass.APPROVAL_DECISION,
            operator=operator,
            request=request,
            path="approval.reveal",
            status_code=_FETCHED_STATUS_CODE if refusal is None else _REFUSED_STATUS_CODE,
            duration_ms=0.0,
            extra_payload={"outcome": "run" if refusal is None else refusal.code},
        )
        await session.commit()
    if refusal is not None:
        raise refusal

    result = await resume_dispatch_after_approval(
        operator=operator, request=request, params=None, requester_fetch=True
    )
    if not isinstance(result, OperationResult):
        raise TypeError(f"unexpected resume result {type(result).__name__}")
    if result.status == "already_resumed":
        raise RevealFetchRefusedError(
            "already_used",
            "The value of this request was already fetched. Send the request again.",
        )
    return result
