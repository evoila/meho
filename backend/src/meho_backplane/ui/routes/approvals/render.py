# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Render helpers for the approvals UI surface.

Initiative #1775 (G10.7 Operator-console hardening), Task #1778. Pulled
out of :mod:`~meho_backplane.ui.routes.approvals.routes` so the projection
+ cookie helpers are unit-testable on their own and the route module stays
under the chassis ~600-line cap.
"""

from __future__ import annotations

from typing import Any

from fastapi.responses import HTMLResponse

from meho_backplane.db.models import ApprovalRequest
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME

__all__ = ["project_request_to_view", "set_csrf_cookie"]


def project_request_to_view(row: ApprovalRequest) -> dict[str, Any]:
    """Project an :class:`ApprovalRequest` ORM row into a template dict.

    The templates render only the operator-relevant subset the issue
    enumerates (op_id, connector_id, ``proposed_effect``, requester
    ``principal_sub``, created_at) plus the id (for the form action URLs).
    Datetimes are pre-formatted to ISO strings here so the templates never
    touch the ORM row (which carries lazy-load + secret-bearing fields like
    ``params`` the modal must never surface). ``proposed_effect`` is passed
    through as the stored dict for the template's ``| tojson`` dump.
    """
    return {
        "id": str(row.id),
        "op_id": row.op_id,
        "connector_id": row.connector_id,
        "principal_sub": row.principal_sub,
        "proposed_effect": row.proposed_effect,
        "created_at": row.created_at.isoformat() if row.created_at else None,
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
