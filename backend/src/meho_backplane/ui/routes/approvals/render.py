# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Render helpers for the approvals UI surface.

Initiative #1775 (G10.7 Operator-console hardening), Task #1778. Extended
by Task #1827 (G10.8) when the surface grew from a pending-only modal to a
full-page console with status-filterable decision history. Pulled out of
:mod:`~meho_backplane.ui.routes.approvals.routes` so the projection +
cookie helpers are unit-testable on their own and the route module stays
under the chassis ~600-line cap.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi.responses import HTMLResponse

from meho_backplane.db.models import ApprovalRequest
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME

__all__ = ["STATUS_PILL_CLASS", "project_request_to_view", "set_csrf_cookie"]

#: DaisyUI badge modifier per request status, for the history view's pill.
#: Pending is the actionable state (warning); approved/rejected are the two
#: terminal decisions (success / error); expired is a quiet timeout
#: (neutral). An unknown status falls back to a plain ghost badge rather
#: than raising, so a future status value degrades to a visible pill
#: instead of a ``StrictUndefined`` template error.
STATUS_PILL_CLASS: Final[dict[str, str]] = {
    "pending": "badge-warning",
    "approved": "badge-success",
    "rejected": "badge-error",
    "expired": "badge-neutral",
}


def project_request_to_view(row: ApprovalRequest) -> dict[str, Any]:
    """Project an :class:`ApprovalRequest` ORM row into a template dict.

    The templates render only the operator-relevant subset: the pending
    detail fields (op_id, connector_id, ``proposed_effect``, requester
    ``principal_sub``, created_at) plus the decided-row shape the history
    view + decided-modal banner need (``status``, ``reviewed_by``,
    ``decided_at``, ``expires_at``, ``work_ref``, ``principal_act``) and the
    id (for the form action URLs). Datetimes are pre-formatted to ISO
    strings here so the templates never touch the ORM row (which carries
    lazy-load + secret-bearing fields).

    The internal ``params`` / ``params_hash`` columns are **never**
    projected: they are the swap-defence + re-dispatch input the approvals
    contract keeps off every read view and broadcast frame
    (``docs/codebase/approvals.md``). The status pill class is resolved by
    the template from :data:`STATUS_PILL_CLASS`, not baked in here, so the
    projection stays presentation-free.
    """
    return {
        "id": str(row.id),
        "op_id": row.op_id,
        "connector_id": row.connector_id,
        "principal_sub": row.principal_sub,
        "principal_act": row.principal_act,
        "proposed_effect": row.proposed_effect,
        "status": row.status,
        "reviewed_by": row.reviewed_by,
        "work_ref": row.work_ref,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
    }


def set_csrf_cookie(response: HTMLResponse, csrf_token: str) -> None:
    """Set the ``meho_csrf`` double-submit cookie on *response*.

    Mirrors the SameSite=Strict + Secure + non-HttpOnly posture every UI
    surface's CSRF cookie carries (HTMX must read it to populate
    ``X-CSRF-Token``; the HMAC binding to the session id defeats the
    cookie-injection vector JS-read would otherwise open). The cookie value
    MUST equal the token the rendered modal echoes via ``hx-headers`` -- the
    CSRF middleware rejects a mismatch.
    """
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )
