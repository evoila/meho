"""The approval request of a runbook check step that waits for approval.

A check call that waits for approval fails its step, and MEHO keeps the id
of the approval request in the step's ``verify_response.actual``
(:data:`APPROVAL_REQUEST_KEY`). When the assignee retries the step, MEHO
reads that request (:func:`read_linked_approval`) instead of running the
call again:

* still ``pending``, or ``approved`` with no stored result yet: the step
  waits again; no new approval request is made;
* ``approved`` with a stored result: the check uses that result, the one
  the approved call returned;
* ``rejected``, ``expired`` or ``withdrawn``, or a request that does not
  belong to the caller or to this check: the check runs the call again.

The read goes through
:func:`~meho_backplane.operations.approval_queue.read_approval_result`, so
only the owner of the request (the caller who made the check wait) reads
its result, in the caller's tenant, and the read writes an
``approval.result`` audit row.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequestStatus

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator

__all__ = ["APPROVAL_REQUEST_KEY", "LinkedApproval", "linked_approval_id", "read_linked_approval"]

#: Key in ``verify_response.actual`` of a check that waits for approval.
APPROVAL_REQUEST_KEY = "approval_request_id"


@dataclass(frozen=True)
class LinkedApproval:
    """The approval request a check waits for, and its result once it ran.

    ``result`` is the stored result of the approved call
    (``OperationResult.model_dump(mode="json")``), or ``None`` while the
    request waits or the approved call has not finished.
    """

    request_id: uuid.UUID
    result: dict[str, Any] | None


def linked_approval_id(verify_response: dict[str, Any] | None) -> uuid.UUID | None:
    """Return the approval request id stored with a check that waits, else ``None``."""
    actual = (verify_response or {}).get("actual")
    if not isinstance(actual, dict) or actual.get("status") != "awaiting_approval":
        return None
    try:
        return uuid.UUID(str(actual[APPROVAL_REQUEST_KEY]))
    except (KeyError, ValueError):
        return None


async def read_linked_approval(
    caller: Operator,
    request_id: uuid.UUID,
    *,
    op_id: str,
    connector_id: str,
) -> LinkedApproval | None:
    """Read the approval request a check waits for, as *caller*.

    Returns ``None`` when the check must run its call again: the request is
    not found in the caller's tenant, belongs to another caller, is for
    another operation, or was rejected, expired or withdrawn.
    """
    # Imported here: ``approval_queue`` imports the runbook modules through
    # the policy and conventions packages.
    from meho_backplane.operations.approval_queue import ApprovalError, read_approval_result

    async with get_sessionmaker()() as session:
        try:
            request = await read_approval_result(session, operator=caller, request_id=request_id)
        except ApprovalError:
            return None
        await session.commit()
    if request.op_id != op_id or request.connector_id != connector_id:
        return None
    if request.status == ApprovalRequestStatus.PENDING.value:
        return LinkedApproval(request_id=request_id, result=None)
    if request.status == ApprovalRequestStatus.APPROVED.value:
        return LinkedApproval(request_id=request_id, result=request.resume_result)
    return None
