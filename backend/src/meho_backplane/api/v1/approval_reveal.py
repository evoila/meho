# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``POST /api/v1/approvals/{id}/reveal``: the person who asked gets the value.

An approved secret reveal runs only here, once, for the person who sent the
request, and only within the fetch window after the approval
(:mod:`meho_backplane.operations.approved_reveal`). The answer holds the
value; MEHO stores the run result without it. ``meho secret read --approval
<id>`` calls this route.

HTTP status codes:

* 200 -- the operation ran. It may be a read, or an operation that changes
  something (for example it creates a token). The body is the run result;
  ``status`` says how it ended (``ok`` with the value, or an error without
  one).
* 403 -- the caller did not send the request.
* 404 -- no such request in the caller's tenant.
* 409 -- the fetch is refused and nothing ran: the request is not a reveal,
  is not approved, was already fetched, its time passed, or its stored params
  no longer match. ``detail`` is ``reveal_<code>: <message>``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict

from meho_backplane.auth.operator import Operator
from meho_backplane.middleware import verify_jwt_and_bind
from meho_backplane.operations.approval_queue import (
    ApprovalNotFoundError,
    ResultAccessForbiddenError,
)
from meho_backplane.operations.approved_reveal import (
    RevealFetchRefusedError,
    fetch_approved_reveal,
)

__all__ = ["router"]

_log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])


class ApprovalRevealResult(BaseModel):
    """The run result of an approved reveal, for the person who asked."""

    model_config = ConfigDict(frozen=True)

    approval_request_id: uuid.UUID
    status: str
    op_id: str
    result: dict[str, Any] | list[Any] | None
    error: str | None
    audit_id: uuid.UUID | None


@router.post("/{request_id}/reveal", response_model=ApprovalRevealResult)
async def reveal_approved_request(
    request_id: Annotated[uuid.UUID, Path()],
    operator: Operator = Depends(verify_jwt_and_bind),
) -> ApprovalRevealResult:
    """Run an approved secret reveal once and return the value to the person who asked."""
    structlog.contextvars.bind_contextvars(
        audit_op_id="approval.reveal",
        # The answer holds a secret value, and the run may change something.
        # So the record of this call is never a plain read.
        audit_op_class="credential_read",
        audit_approval_request_id=str(request_id),
    )
    try:
        result = await fetch_approved_reveal(operator, request_id)
    except ApprovalNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="approval_request_not_found"
        ) from exc
    except ResultAccessForbiddenError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN, detail="not_request_owner"
        ) from exc
    except RevealFetchRefusedError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT, detail=f"reveal_{exc.code}: {exc.message}"
        ) from exc
    # Log only fields that hold no secret value.
    _log.info(
        "approval_reveal_fetched",
        approval_request_id=str(request_id),
        op_id=result.op_id,
        status=result.status,
        operator_sub=operator.sub,
    )
    return ApprovalRevealResult(
        approval_request_id=request_id,
        status=result.status,
        op_id=result.op_id,
        result=result.result,
        error=result.error,
        audit_id=result.audit_id,
    )
