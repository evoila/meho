# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Cancel-trigger confirm-modal render + submit (Task #1826).

Initiative #1824 (G10.8 Autonomous execution control plane), Task #1826
(T6). Cancel is the scheduler's emergency stop: the transition is
**terminal** -- a cancelled trigger never fires again, and there is no
un-cancel path (per
:class:`~meho_backplane.db.models.ScheduledTriggerStatus`). So the UI
fronts it with a strong native-``<dialog>`` confirm that spells out the
irreversibility before the operator commits.

Two concerns, both tenant_admin-gated server-side:

* :func:`render_cancel_modal` -- ``GET /ui/scheduler/{id}/cancel``. Loads
  the trigger (404 on cross-tenant / absent), then renders the confirm
  dialog naming the trigger + its schedule and stating that the row is
  kept for audit but will never fire again. Mints + re-sets the CSRF
  cookie so the dialog's submit echo lines up (#1693 / #1754).

* :func:`submit_cancel` -- ``POST /ui/scheduler/{id}/cancel``. Cancels via
  the in-process :class:`~meho_backplane.scheduler.service.SchedulerAdminService`
  (the same ``get`` pre-flight + ``cancel`` the Bearer ``DELETE`` route
  uses) so the UI write and the REST write share one audit + state-machine
  code path. The two service-level edge cases the REST surface distinguishes
  are handled here too: a cross-tenant / absent id (404
  ``trigger_not_found``) and an already-``fired`` terminal trigger (409
  ``trigger_already_fired``). Both re-render the confirm dialog with a
  typed banner rather than tearing the operator out of the flow. Success
  -> 204 + ``HX-Redirect: /ui/scheduler``.
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import HTTPException, Request, status
from fastapi.responses import HTMLResponse

from meho_backplane.auth.operator import Operator
from meho_backplane.scheduler.schemas import ScheduledTriggerRead
from meho_backplane.scheduler.service import SchedulerAdminService
from meho_backplane.ui.auth.middleware import UISessionContext
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.routes.scheduler.views import coerce_utc_aware
from meho_backplane.ui.templating import get_templates

__all__ = ["render_cancel_modal", "submit_cancel"]

_log = structlog.get_logger(__name__)


def _set_csrf_cookie(response: HTMLResponse, csrf_token: str) -> None:
    """Re-set the ``meho_csrf`` cookie on a modal-rendering response (#1693)."""
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )


def _schedule_summary(trigger: ScheduledTriggerRead) -> str:
    """One-line schedule descriptor for the confirm dialog body."""
    if trigger.cron_expr is not None:
        return f"cron: {trigger.cron_expr} ({trigger.timezone})"
    if trigger.fire_at is not None:
        coerced = coerce_utc_aware(trigger.fire_at)
        assert coerced is not None
        return f"one-off: {coerced.isoformat()}"
    return "event-driven"


async def _load_trigger_or_404(
    session_ctx: UISessionContext, trigger_id: uuid.UUID
) -> ScheduledTriggerRead:
    """Fetch one trigger, tenant-isolated, mapping absence to 404."""
    scheduler = SchedulerAdminService()
    trigger = await scheduler.get(session_ctx.tenant_id, trigger_id)
    if trigger is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="trigger_not_found",
        )
    return trigger


async def render_cancel_modal(
    request: Request,
    session_ctx: UISessionContext,
    trigger_id: uuid.UUID,
    *,
    error_message: str | None = None,
) -> HTMLResponse:
    """Render the terminal-cancel confirm dialog for one trigger.

    The dialog names the trigger + schedule and states the irreversibility
    explicitly (the cancel is permanent; the row is kept for audit; the
    schedule will never fire again). The ``error_message`` argument lets
    :func:`submit_cancel` re-render this dialog with a typed banner (e.g.
    a 409 ``trigger_already_fired``) without a second round-trip.
    """
    trigger = await _load_trigger_or_404(session_ctx, trigger_id)
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, object] = {
        "trigger_id": str(trigger.id),
        "schedule_summary": _schedule_summary(trigger),
        "status": trigger.status.value,
        "csrf_token": csrf_token,
        "error_message": error_message,
    }
    response = get_templates().TemplateResponse(request, "scheduler/_cancel_modal.html", context)
    _set_csrf_cookie(response, csrf_token)
    return response


def _bind_audit_for_cancel(*, session_ctx: UISessionContext, trigger_id: uuid.UUID) -> None:
    """Bind contextvars so the chassis audit hook commits a ``scheduler.cancel`` row.

    Mirrors the Bearer ``DELETE /api/v1/scheduler/triggers/{id}`` binding
    (the UI session middleware does not bind these itself).
    """
    structlog.contextvars.bind_contextvars(
        operator_sub=session_ctx.operator_sub,
        tenant_id=str(session_ctx.tenant_id),
        audit_op_id="scheduler.cancel",
        audit_op_class="write",
        audit_trigger_id=str(trigger_id),
    )


async def submit_cancel(
    request: Request,
    session_ctx: UISessionContext,
    operator: Operator,
    trigger_id: uuid.UUID,
) -> HTMLResponse:
    """Cancel one trigger (terminal); HX-Redirect to the list on success.

    Mirrors the Bearer ``DELETE`` route's get-then-cancel sequence so the
    404 (absent / cross-tenant) and 409 (already ``fired``) edge cases are
    distinguished identically. The state machine (``cancel`` matches
    ``active`` / ``paused`` only; an already-cancelled trigger is an
    idempotent success) lives in the service.
    """
    # Pre-flight existence + tenant-isolation check (404 on miss). The
    # operator argument carries the tenant_admin-verified identity from
    # ``resolve_operator_or_403``; tenant scoping still flows through the
    # session ctx so the read + write target the same tenant.
    await _load_trigger_or_404(session_ctx, trigger_id)

    _bind_audit_for_cancel(session_ctx=session_ctx, trigger_id=trigger_id)

    service = SchedulerAdminService()
    cancelled = await service.cancel(operator.tenant_id, trigger_id)
    if not cancelled:
        # The existence pre-flight passed, so the only way to land here is
        # the row being in terminal ``fired`` state (the lifecycle is
        # ``fired`` -> end, never ``fired`` -> ``cancelled``). Re-render the
        # confirm dialog with the typed banner.
        _log.info(
            "ui_scheduler_cancel_conflict",
            tenant_id=str(session_ctx.tenant_id),
            trigger_id=str(trigger_id),
        )
        return await render_cancel_modal(
            request,
            session_ctx,
            trigger_id,
            error_message=(
                "This trigger has already fired and cannot be cancelled "
                "(a fired one-off is terminal)."
            ),
        )

    _log.info(
        "ui_scheduler_cancel",
        tenant_id=str(session_ctx.tenant_id),
        operator_sub=session_ctx.operator_sub,
        trigger_id=str(trigger_id),
    )
    return HTMLResponse(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"HX-Redirect": "/ui/scheduler"},
    )
