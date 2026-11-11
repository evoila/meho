# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The "Show value" button of a waiting secret read in the Vault console.

``POST /ui/vault/read/reveal`` runs the approved read once for the person
who asked, through
:func:`~meho_backplane.operations.approved_reveal.fetch_approved_reveal`,
and renders the value like ``/ui/vault/read``. It is a POST because it uses
up the one-time permission; the CSRF middleware checks it. A request of
another person, of another tenant or of another operation is a 404. A
refused fetch (not approved yet, already fetched, time passed) shows its
reason and no value. MEHO stores no value; this page never logs one. When
the read ran (also when it ended with an error and shows no value), the page
says ``Cache-Control: no-store`` and ``Pragma: no-cache``
(:mod:`meho_backplane.no_store`), so no browser or proxy keeps the value.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

import structlog
from fastapi import HTTPException, Request, status
from fastapi.responses import HTMLResponse

from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.no_store import set_no_store
from meho_backplane.operations.approval_queue import (
    ApprovalNotFoundError,
    ResultAccessForbiddenError,
    get_request,
)
from meho_backplane.operations.approved_reveal import (
    RevealFetchRefusedError,
    fetch_approved_reveal,
)
from meho_backplane.ui.auth.middleware import UISessionContext
from meho_backplane.ui.csrf import mint_csrf_token
from meho_backplane.ui.routes.approvals.render import set_csrf_cookie
from meho_backplane.ui.templating import get_templates

__all__ = ["render_approved_reveal", "with_csrf_cookie"]

log = structlog.get_logger(__name__)

_OP_READ: Final[str] = "vault.kv.read"
_DEFAULT_MOUNT: Final[str] = "secret"


def with_csrf_cookie(response: HTMLResponse, session: UISessionContext) -> HTMLResponse:
    """Set the CSRF cookie the "Show value" POST needs on *response*."""
    set_csrf_cookie(response, mint_csrf_token(str(session.session_id)))
    return response


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="approval_request_not_found")


async def render_approved_reveal(
    request: Request,
    session: UISessionContext,
    operator: Operator,
    request_id: uuid.UUID,
) -> HTMLResponse:
    """Run the approved ``vault.kv.read`` *request_id* once and render the value."""
    try:
        async with get_sessionmaker()() as db:
            row = await get_request(db, tenant_id=operator.tenant_id, request_id=request_id)
    except ApprovalNotFoundError as exc:
        raise _not_found() from exc
    if row.op_id != _OP_READ or row.principal_sub != operator.sub:
        raise _not_found()
    params = row.params or {}
    ran = False
    try:
        result = await fetch_approved_reveal(operator, request_id)
        envelope: dict[str, Any] = result.model_dump(mode="json")
        ran = True
    except (ApprovalNotFoundError, ResultAccessForbiddenError) as exc:
        raise _not_found() from exc
    except RevealFetchRefusedError as exc:
        envelope = {
            "status": "awaiting_approval" if exc.code == "not_approved" else "error",
            "op_id": _OP_READ,
            "error": exc.message,
            "extras": {
                "approval_request_id": str(row.id),
                "approval_status": row.status,
                "error_code": f"reveal_{exc.code}",
            },
        }
    # Log ONLY fields that hold no secret value.
    log.info(
        "ui_vault_read_reveal",
        approval_request_id=str(row.id),
        approval_status=row.status,
        status=envelope.get("status"),
        tenant_id=str(operator.tenant_id),
    )
    context: dict[str, Any] = {
        "error": None,
        "envelope": envelope,
        "mount": str(params.get("mount") or _DEFAULT_MOUNT),
        "path": str(params.get("path") or ""),
        "target": "",
    }
    response = get_templates().TemplateResponse(request, "vault/_secret.html", context)
    if ran:
        set_no_store(response)
    return with_csrf_cookie(response, session)
