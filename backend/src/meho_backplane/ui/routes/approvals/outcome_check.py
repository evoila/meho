# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Console: mark an approved run with an unknown outcome as checked (#5026).

``POST /ui/approvals/{id}/outcome-check`` records what the person found on
the target ("it happened" or "it did not happen") with a note, through
:func:`~meho_backplane.operations.approval_outcome_check.mark_outcome_checked`,
the same service the REST route calls. Nothing runs. The detail modal is
rendered again: on success it shows the recorded check, on a refusal it
shows why.
"""

from __future__ import annotations

import uuid
from typing import Final

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations.approval_outcome_check import (
    OutcomeAlreadyCheckedError,
    OutcomeCheckVerdict,
    OutcomeNotUnknownError,
    mark_outcome_checked,
)
from meho_backplane.operations.approval_queue import (
    MAX_REASON_LENGTH,
    ApprovalNotFoundError,
    UnauthorizedApprovalError,
)
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.routes.approvals import routes as _routes

__all__ = ["build_outcome_check_router"]

_require_session = Depends(require_ui_session)

#: The refusal text per service error. The approval-not-found case is not
#: here: the modal cannot be shown for it, so it stays a 404.
_REFUSALS: Final[dict[type[Exception], tuple[int, str]]] = {
    UnauthorizedApprovalError: (
        403,
        "Your role cannot mark approval requests as checked "
        "(operator role or the approver capability required).",
    ),
    OutcomeAlreadyCheckedError: (409, "Someone already marked this request as checked."),
    OutcomeNotUnknownError: (
        409,
        "This request has no unknown outcome to check. Its run has a known "
        "result, or it started only a short time ago.",
    ),
    ValueError: (422, "Write what you checked in the note."),
}


def build_outcome_check_router() -> APIRouter:
    """Build the ``/ui/approvals/{id}/outcome-check`` route."""
    router = APIRouter(tags=["ui-approvals"])

    @router.post("/ui/approvals/{request_id}/outcome-check", response_class=HTMLResponse)
    async def approval_outcome_check(
        request: Request,
        request_id: uuid.UUID,
        session: UISessionContext = _require_session,
        verdict: OutcomeCheckVerdict = Form(),
        note: str = Form(default="", max_length=MAX_REASON_LENGTH),
    ) -> HTMLResponse:
        """Record the check, then render the detail modal again."""
        operator = await _routes._resolve_operator(session)
        try:
            async with get_sessionmaker()() as db_session:
                await mark_outcome_checked(
                    db_session, request_id, operator=operator, verdict=verdict, note=note
                )
                await db_session.commit()
        except ApprovalNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="approval_request_not_found"
            ) from exc
        except tuple(_REFUSALS) as exc:
            code, message = next(v for k, v in _REFUSALS.items() if isinstance(exc, k))
            return await _routes._render_detail_modal(
                request, session, request_id, error_status=code, error_message=message
            )
        response = await _routes._render_detail_modal(request, session, request_id)
        # The open history list fetches the active tab again, so the checked
        # request leaves the "Outcome unknown" tab. The modal stays open and
        # shows the recorded check.
        response.headers["HX-Trigger"] = "meho:approval-bump"
        return response

    return router
