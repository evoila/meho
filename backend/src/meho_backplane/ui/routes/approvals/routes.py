# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approvals UI routes: a notifications bell + approve/deny modal over a session BFF.

Initiative #1775 (G10.7 Operator-console hardening), Task #1778.

The approval queue, its decision endpoints, and the lifecycle broadcasts
already exist (Goal #800). This surface is the operator-console face of
them: a bell/badge in the app-shell, fed live by the SSE bridge, opening a
modal that approves or denies a pending request.

Why a session BFF and not the Bearer ``/api/v1/approvals/*`` routes
-------------------------------------------------------------------

The REST approval routes (``api/v1/approvals.py``) are Bearer-gated
(``_require_operator`` -> ``require_role(TenantRole.OPERATOR)`` over a
verified JWT). A browser carrying only the BFF session cookie + the CSRF
double-submit token cannot authenticate them. So this module adds
``/ui/approvals`` sub-routes that are ``require_ui_session`` + CSRF-gated
and call the :mod:`~meho_backplane.operations.approval_queue` **service**
in-process (``list_pending`` / ``get_request`` / ``approve_request`` /
``reject_request``) -- the same console-surface pattern the corpus / kb /
memory surfaces use. The in-process call keeps the synchronous-audit
binding and avoids a self-HTTP hop that the cookie could not auth anyway.

Route inventory
---------------

* ``GET /ui/approvals/badge`` -- the live pending count, rendered as the
  bell badge fragment. The app-shell bell loads it on ``hx-trigger="load"``
  to seed the count authoritatively; the SSE stream keeps it live after.
  Always pending-only -- it counts *actionable* work, never decided rows.
* ``GET /ui/approvals`` -- content-negotiated (#1827). A normal navigation
  (sidebar link, bookmark, hard-refresh -- no ``HX-Request`` header)
  renders the **full-page console** (``extends base.html``): status tabs
  (Pending / Approved / Rejected / Expired / All), a work_ref filter, and
  the decision-history list. The bell's ``hx-get`` carries ``HX-Request:
  true`` and keeps getting the existing **pending panel** modal fragment,
  so the bell-click flow is unchanged.
* ``GET /ui/approvals/list`` -- the decision-history partial (#1827): the
  HTMX swap target for the status tabs / work_ref filter / "load more"
  offset pager on the full page. Reuses ``list_pending`` with an explicit
  ``status`` (``None`` for the All tab), an optional ``work_ref``, and a
  real ``offset`` pager (not the badge's 50-row glance cap).
* ``GET /ui/approvals/{id}`` -- the request-detail modal fragment
  (op_id / connector_id / proposed_effect / requester principal_sub /
  created_at) with Approve + Deny buttons. A **decided** row (#1827)
  renders read-only: a decision banner ("Approved by X" / "Rejected by X")
  and no Approve/Deny actions. Mints + re-sets the ``meho_csrf`` cookie so
  the modal's own ``hx-headers`` echo lines up with the cookie (the modal
  render rotates it -- #1693 / #1754).
* ``POST /ui/approvals/{id}/approve`` -- approve in-process, re-dispatch
  the parked op, publish the fail-open broadcast.
* ``POST /ui/approvals/{id}/reject`` -- reject in-process, publish.

The ``params`` / ``params_hash`` columns are internal (swap-defence +
re-dispatch input) and are **never** projected onto any of these views or
the badge -- the projection in :mod:`.render` omits them by construction.

Self-approval invariant (#1401)
-------------------------------

The **Approve** button is disabled in the modal when
``operator.sub == request.principal_sub`` and the deployment has not
enabled the audited single-operator break-glass
(``Settings.approval_allow_self_approval``). **Deny** stays enabled (an
operator withdrawing their own request is not an escalation). The button
state is UX only -- the BFF re-checks server-side: ``approve_request`` ->
``_check_self_approval`` raises :class:`SelfApprovalForbiddenError`, which
this module maps to a 403 surfaced in the re-rendered modal.

Tenant isolation
----------------

Every read + decision derives ``tenant_id`` from the validated
:class:`UISessionContext` only -- never a query / form field. The service
layer (``list_pending`` WHERE clause, ``_load_for_tenant``) makes a
cross-tenant id indistinguishable from a missing one (404), so the bell +
modal only ever surface this session's tenant's requests.

CSRF
----

The approve/deny POSTs carry the OWASP signed double-submit token on the
button's own ``hx-headers`` (HTMX does not inherit ``hx-headers`` to
children -- the form/button that issues the request must carry it). The
detail-modal GET mints a fresh token and re-sets the ``meho_csrf`` cookie
on the same response so the pair always lines up after the modal swap. The
decision POSTs do **not** re-render that form (they swap the modal body to
a terminal "decided" fragment), so they leave the cookie untouched.
"""

from __future__ import annotations

import uuid
from typing import Final

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse

from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.operations.approval_queue import (
    ApprovalNotFoundError,
    ApprovalRequestAlreadyDecidedError,
    SelfApprovalForbiddenError,
    UnauthorizedApprovalError,
    approve_request,
    get_request,
    list_pending,
    publish_approval_event,
    reject_request,
    resume_dispatch_after_approval,
)
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.auth.refresh import (
    load_fresh_session,
    verify_access_token_with_refresh,
)
from meho_backplane.ui.csrf import mint_csrf_token
from meho_backplane.ui.routes.approvals.render import (
    STATUS_PILL_CLASS,
    project_request_to_view,
    set_csrf_cookie,
)
from meho_backplane.ui.templating import get_templates

__all__ = ["build_approvals_router"]

log = structlog.get_logger(__name__)

#: Hard cap on the pending requests the panel + badge consider. The bell
#: is a glance surface; an operator with hundreds of unactioned requests
#: has a queue-management problem the bell isn't the answer to. 50 matches
#: the ``list_pending`` default and the dashboard feed-tray DOM cap.
_PENDING_LIMIT: Final[int] = 50

#: Page size for the full-page decision-history list (#1827). The history
#: view is a *browse* surface, not a glance: it pages through every status
#: with a real offset pager, so it must not inherit the badge's 50-row
#: glance cap (which would silently truncate the history). One page is kept
#: small so the first paint is fast; "Load more" advances the offset.
_HISTORY_PAGE_SIZE: Final[int] = 25

#: Maximum work_ref length accepted by the history filter. ``work_ref`` is
#: an opaque external change-ticket string (e.g. ``"gh:evoila/meho#1"``);
#: bounding the wire shape rejects an oversized paste at the form boundary
#: (FastAPI 422) rather than forwarding it into the WHERE clause.
_MAX_WORK_REF_LENGTH: Final[int] = 512

#: The status-tab vocabulary the full-page console exposes. Each entry maps
#: a tab key the template renders to the ``list_pending`` ``status``
#: argument: the four closed ``ApprovalRequestStatus`` values plus an "all"
#: tab that passes ``status=None`` (every state). A query value outside this
#: set is rejected as an unknown tab (422) rather than silently coerced, so
#: a typo'd URL fails loud instead of returning a misleading filtered view.
_HISTORY_TABS: Final[dict[str, str | None]] = {
    "pending": "pending",
    "approved": "approved",
    "rejected": "rejected",
    "expired": "expired",
    "all": None,
}

#: Default tab when none is supplied. Pending is the actionable queue, so a
#: bare ``/ui/approvals`` lands the operator on the work that needs them.
_DEFAULT_HISTORY_TAB: Final[str] = "pending"

#: Optional human reason length accepted on the deny form. The audit row
#: stores it verbatim; bounding the wire shape protects the form-body
#: parse against a paste-from-clipboard accident.
_MAX_REASON_LENGTH: Final[int] = 2000

#: Module-level ``Depends`` closure for the operator-session gate. Built
#: once (rather than inline) to satisfy ruff B008, matching the convention
#: the corpus / kb / topology / dashboard routes established.
_require_session = Depends(require_ui_session)


async def _resolve_operator(session: UISessionContext) -> Operator:
    """Reconstruct the full :class:`Operator` from the BFF session.

    The approval-queue service needs a real :class:`Operator` (it reads
    ``operator.tenant_role`` for the role gate and ``operator.sub`` for the
    self-approval guard + the audit + broadcast principal). The
    :class:`UISessionContext` the middleware hands route handlers carries
    only ``operator_sub`` / ``tenant_id``, so -- mirroring the corpus
    surface and :func:`~meho_backplane.ui.auth.middleware.require_ui_admin`
    -- this loads the decrypted session and presents its (silently
    refreshed) access token to the chassis JWT chain via
    :func:`~meho_backplane.ui.auth.refresh.verify_access_token_with_refresh`.

    Raises :class:`fastapi.HTTPException` 401 when the session was revoked /
    expired between the middleware check and here (the BFF error handler
    maps the ``ui_session_required`` detail to a login redirect for HTML
    requests).
    """
    decrypted = await load_fresh_session(session.session_id)
    if decrypted is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="ui_session_required",
        )
    settings = get_settings()
    _refreshed, operator = await verify_access_token_with_refresh(
        decrypted,
        expected_audience=settings.keycloak_audience,
    )
    return operator


def _is_htmx(request: Request) -> bool:
    """Return whether *request* is an HTMX-driven fetch.

    HTMX 2 sets ``HX-Request: true`` on every directive-driven fetch
    (bell-click ``hx-get``, status-tab swap, pager). Case-insensitive read
    matching the topology / memory / runbooks surfaces' helper. The bell's
    modal-open ``hx-get /ui/approvals`` is the one HTMX caller of the index
    route; a normal navigation (sidebar link, bookmark, hard-refresh) omits
    the header and gets the full page instead.
    """
    return request.headers.get("hx-request", "").lower() == "true"


async def _list_pending_for_session(session: UISessionContext) -> list[ApprovalRequest]:
    """List this session tenant's pending approval requests (newest-first).

    Tenant-isolated by the ``list_pending`` WHERE clause -- the tenant id
    comes from the validated session only, so cross-tenant requests are
    invisible. Bounded to :data:`_PENDING_LIMIT`.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db_session:
        return await list_pending(
            db_session,
            tenant_id=session.tenant_id,
            status="pending",
            limit=_PENDING_LIMIT,
        )


async def _list_history_for_session(
    session: UISessionContext,
    *,
    status_filter: str | None,
    work_ref: str | None,
    offset: int,
) -> tuple[list[ApprovalRequest], bool]:
    """Page through this tenant's approval history (newest-first).

    Reuses the same ``list_pending`` substrate the badge + panel call, but
    with an explicit ``status`` (``None`` for the All tab) + ``work_ref``
    filter + a real ``offset`` pager. Tenant-isolated by the service WHERE
    clause -- the tenant id comes from the validated session only.

    Over-fetches one row (``limit + 1``) to detect a further page without a
    second count query, then trims to the page size. Returns
    ``(rows, has_more)``; ``has_more`` drives the "Load more" affordance.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db_session:
        rows = await list_pending(
            db_session,
            tenant_id=session.tenant_id,
            status=status_filter,
            work_ref=work_ref,
            limit=_HISTORY_PAGE_SIZE + 1,
            offset=offset,
        )
    has_more = len(rows) > _HISTORY_PAGE_SIZE
    return rows[:_HISTORY_PAGE_SIZE], has_more


async def _get_request_or_404(session: UISessionContext, request_id: uuid.UUID) -> ApprovalRequest:
    """Fetch one approval request, tenant-isolated, mapping absence to 404."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db_session:
        try:
            return await get_request(
                db_session,
                tenant_id=session.tenant_id,
                request_id=request_id,
            )
        except ApprovalNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="approval_request_not_found",
            ) from exc


def _is_self_approval_blocked(operator: Operator, request: ApprovalRequest) -> bool:
    """Return whether *operator* is barred from approving *request* in the UI.

    Mirrors the service-layer
    :func:`~meho_backplane.operations.approval_queue._check_self_approval`
    contract exactly so the disabled-button state matches the server's
    decision: blocked iff the approver is the requester
    (``operator.sub == request.principal_sub``) **and** the audited
    single-operator break-glass is off
    (``Settings.approval_allow_self_approval``). The button state is UX
    only; the service re-checks on the POST (never trust the disabled
    button), so a forged self-approve still 403s.
    """
    if operator.sub != request.principal_sub:
        return False
    return not get_settings().approval_allow_self_approval


def build_approvals_router() -> APIRouter:
    """Construct the ``/ui/approvals*`` :class:`APIRouter`.

    Factory function (not a module-level constant) so a test app can
    construct parallel routers without shared route state -- the same
    convention every surface router (kb / corpus / memory / topology)
    follows. Registered ahead of the stubs aggregate in
    :func:`meho_backplane.ui.routes.build_router`; the literal
    ``badge`` / ``approve`` / ``reject`` segments are registered so they
    never bind as the ``{request_id}`` parameter.
    """
    router = APIRouter(tags=["ui-approvals"])

    # NOTE: the literal ``badge`` / ``list`` segments are registered BEFORE
    # the ``/ui/approvals/{request_id}`` slug route so first-match-wins
    # routing never binds them as the request-id parameter -- the ordering
    # discipline the kb / corpus routers document. ``approve`` / ``reject``
    # are POST, so they never collide with the GET slug route.

    @router.get("/ui/approvals/badge", response_class=HTMLResponse)
    async def approvals_badge(
        request: Request,
        session: UISessionContext = _require_session,
    ) -> HTMLResponse:
        """Render the live pending-count badge fragment (delegates below)."""
        return await _render_badge(request, session)

    @router.get("/ui/approvals/list", response_class=HTMLResponse)
    async def approvals_history(
        request: Request,
        session: UISessionContext = _require_session,
        tab: str = Query(default=_DEFAULT_HISTORY_TAB),
        work_ref: str = Query(default="", max_length=_MAX_WORK_REF_LENGTH),
        offset: int = Query(default=0, ge=0),
    ) -> HTMLResponse:
        """Render the decision-history list partial (delegates below)."""
        return await _render_history(request, session, tab, work_ref, offset)

    @router.get("/ui/approvals", response_class=HTMLResponse)
    async def approvals_index(
        request: Request,
        session: UISessionContext = _require_session,
        tab: str = Query(default=_DEFAULT_HISTORY_TAB),
        work_ref: str = Query(default="", max_length=_MAX_WORK_REF_LENGTH),
        offset: int = Query(default=0, ge=0),
    ) -> HTMLResponse:
        """Render the approvals surface, content-negotiated by ``HX-Request``.

        The bell's modal-open ``hx-get`` (``HX-Request: true``) gets the
        existing pending **panel** fragment unchanged; a normal navigation
        gets the full-page **console** with the status tabs + history.
        """
        if _is_htmx(request):
            return await _render_panel(request, session)
        return await _render_index(request, session, tab, work_ref, offset)

    @router.get("/ui/approvals/{request_id}", response_class=HTMLResponse)
    async def approval_detail(
        request: Request,
        request_id: uuid.UUID,
        session: UISessionContext = _require_session,
    ) -> HTMLResponse:
        """Render the request-detail + approve/deny modal (delegates below)."""
        return await _render_detail_modal(request, session, request_id)

    @router.post("/ui/approvals/{request_id}/approve", response_class=HTMLResponse)
    async def approval_approve(
        request: Request,
        request_id: uuid.UUID,
        session: UISessionContext = _require_session,
        reason: str = Form(default="", max_length=_MAX_REASON_LENGTH),
    ) -> HTMLResponse:
        """Approve a pending request in-process (delegates below)."""
        return await _decide(request, session, request_id, decision="approved", reason=reason)

    @router.post("/ui/approvals/{request_id}/reject", response_class=HTMLResponse)
    async def approval_reject(
        request: Request,
        request_id: uuid.UUID,
        session: UISessionContext = _require_session,
        reason: str = Form(default="", max_length=_MAX_REASON_LENGTH),
    ) -> HTMLResponse:
        """Reject a pending request in-process (delegates below)."""
        return await _decide(request, session, request_id, decision="rejected", reason=reason)

    return router


async def _render_badge(request: Request, session: UISessionContext) -> HTMLResponse:
    """Render the bell badge fragment with the live pending count.

    Loaded on ``hx-trigger="load"`` by the app-shell bell so the count is
    authoritative on first paint (the SSE stream only carries *deltas* +
    a recent backlog, not the full pending set). Tenant-scoped via the
    session.
    """
    pending = await _list_pending_for_session(session)
    context: dict[str, object] = {"pending_count": len(pending)}
    return get_templates().TemplateResponse(request, "approvals/_badge.html", context)


async def _render_panel(request: Request, session: UISessionContext) -> HTMLResponse:
    """Render the pending-requests panel (the modal body listing the queue)."""
    pending = await _list_pending_for_session(session)
    context: dict[str, object] = {
        "requests": [project_request_to_view(row) for row in pending],
    }
    return get_templates().TemplateResponse(request, "approvals/_panel.html", context)


def _resolve_history_tab(tab: str) -> str:
    """Validate a status-tab key, mapping an unknown one to a 422.

    The query value must be one of :data:`_HISTORY_TABS`. A foreign value
    (a typo'd bookmark, a hand-edited URL) fails loud rather than silently
    coercing to a default that would render a misleading filtered view.
    """
    if tab not in _HISTORY_TABS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unknown approvals tab '{tab}'.",
        )
    return tab


async def _build_history_context(
    session: UISessionContext,
    tab: str,
    work_ref: str,
    offset: int,
) -> dict[str, object]:
    """Assemble the shared context for the history page + its list partial.

    Both the full-page render and the HTMX list-swap render the same row
    projection + pager state, so they share this builder -- the markup is
    identical across a direct navigation and a tab/pager swap. The
    ``work_ref`` filter is applied only when non-blank (a blank box means
    "no work_ref filter", i.e. every request for the active tab).
    """
    tab = _resolve_history_tab(tab)
    work_ref_filter = work_ref.strip() or None
    rows, has_more = await _list_history_for_session(
        session,
        status_filter=_HISTORY_TABS[tab],
        work_ref=work_ref_filter,
        offset=offset,
    )
    return {
        "requests": [project_request_to_view(row) for row in rows],
        "status_pill_class": STATUS_PILL_CLASS,
        "active_tab": tab,
        "tabs": list(_HISTORY_TABS.keys()),
        "work_ref": work_ref.strip(),
        "offset": offset,
        "page_size": _HISTORY_PAGE_SIZE,
        "next_offset": offset + _HISTORY_PAGE_SIZE,
        "has_more": has_more,
    }


async def _render_index(
    request: Request,
    session: UISessionContext,
    tab: str,
    work_ref: str,
    offset: int,
) -> HTMLResponse:
    """Render the full-page approvals console for a normal navigation.

    Extends ``base.html`` (chrome + sidebar highlight) and seeds the status
    tabs + work_ref filter + first history page. The status-tab/pager swaps
    re-fetch ``GET /ui/approvals/list`` (the partial) into the list region.
    No CSRF cookie is set here: this surface is read-only (the decision
    POSTs live on the detail modal, which mints its own token).
    """
    context = await _build_history_context(session, tab, work_ref, offset)
    context["active_surface"] = "approvals"
    context["page_title"] = "Approvals"
    return get_templates().TemplateResponse(request, "approvals/index.html", context)


async def _render_history(
    request: Request,
    session: UISessionContext,
    tab: str,
    work_ref: str,
    offset: int,
) -> HTMLResponse:
    """Render the decision-history list partial for the HTMX tab/pager swap."""
    context = await _build_history_context(session, tab, work_ref, offset)
    return get_templates().TemplateResponse(request, "approvals/_history.html", context)


async def _render_detail_modal(
    request: Request,
    session: UISessionContext,
    request_id: uuid.UUID,
    *,
    error_status: int | None = None,
    error_message: str | None = None,
) -> HTMLResponse:
    """Render the request-detail modal with Approve / Deny.

    Computes the self-approval disabled-state for the Approve button
    (#1401) against the reconstructed operator, mints a fresh CSRF token,
    and re-sets the ``meho_csrf`` cookie on the response so the modal's own
    ``hx-headers`` echo lines up with the cookie after the swap (the modal
    render rotates the token -- the #1693 / #1754 cookie-desync class). The
    ``error_*`` arguments let the decision handlers re-render this modal
    with a typed banner (e.g. a server-side self-approval 403) without a
    second round-trip.
    """
    operator = await _resolve_operator(session)
    approval = await _get_request_or_404(session, request_id)

    self_approval_blocked = _is_self_approval_blocked(operator, approval)
    csrf_token = mint_csrf_token(str(session.session_id))
    context: dict[str, object] = {
        "request": project_request_to_view(approval),
        "self_approval_blocked": self_approval_blocked,
        "self_approval_setting": "APPROVAL_ALLOW_SELF_APPROVAL",
        "operator_sub": operator.sub,
        "status_pill_class": STATUS_PILL_CLASS,
        "csrf_token": csrf_token,
        "error_status": error_status,
        "error_message": error_message,
    }
    response = get_templates().TemplateResponse(request, "approvals/_modal.html", context)
    set_csrf_cookie(response, csrf_token)
    return response


async def _decide(
    request: Request,
    session: UISessionContext,
    request_id: uuid.UUID,
    *,
    decision: str,
    reason: str,
) -> HTMLResponse:
    """Approve or reject a pending request in-process; render the outcome.

    On success the modal body swaps to the terminal ``_decided`` fragment
    and an ``HX-Trigger`` response header fires ``meho:approval-decided`` so
    the app-shell bell decrements its count and closes the dialog (the
    issue's "modal closes and the badge decrements"). An approve additionally
    re-dispatches the parked op via the shared
    :func:`~meho_backplane.operations.approval_queue.resume_dispatch_after_approval`
    helper (the existing approve path), exactly as the REST route does.

    A recoverable decision failure (self-approval 403, already-decided 409,
    role 403, not-found 404) re-renders the detail modal with a typed banner
    rather than tearing the operator out of the flow.
    """
    operator = await _resolve_operator(session)
    try:
        approval = await _commit_decision(
            operator=operator, request_id=request_id, decision=decision, reason=reason
        )
    except HTTPException as exc:
        # Recoverable: re-render the modal with the typed error banner so
        # the operator sees *why* (e.g. the server-side self-approval
        # block they bypassed by forging the disabled button). 404 is the
        # one case where re-rendering the detail would 404 again, so it
        # surfaces as the panel's empty/refreshed state instead.
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            raise
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return await _render_detail_modal(
            request,
            session,
            request_id,
            error_status=exc.status_code,
            error_message=detail,
        )

    # Approve re-dispatches the parked op (the committed approval is the
    # authorization). Reject does not. Mirrors the REST /approve route; the
    # service helper owns target re-hydration + the fail-closed branches.
    if decision == "approved":
        dispatch_result = await resume_dispatch_after_approval(
            operator=operator, request=approval, params=None
        )
        log.info(
            "ui_approval_redispatched",
            approval_request_id=str(request_id),
            op_id=approval.op_id,
            dispatch_status=dispatch_result.status,
            operator_sub=operator.sub,
        )

    context: dict[str, object] = {
        "request": project_request_to_view(approval),
        "decision": decision,
    }
    response = get_templates().TemplateResponse(request, "approvals/_decided.html", context)
    # Drive the app-shell bell: decrement the live count + close the modal.
    response.headers["HX-Trigger"] = "meho:approval-decided"
    return response


async def _commit_decision(
    *,
    operator: Operator,
    request_id: uuid.UUID,
    decision: str,
    reason: str,
) -> ApprovalRequest:
    """Run approve/reject in one transaction + publish; map errors to HTTP.

    The self-approval guard, role gate, tenant-isolation 404, and
    already-decided 409 all live in the service
    (:func:`~meho_backplane.operations.approval_queue.approve_request` /
    :func:`reject_request`); this projects each onto the same HTTP status
    the REST surface uses so the modal can render a consistent banner. The
    fail-open broadcast publishes only AFTER the commit (a phantom event
    cannot outlive a failed transaction).
    """
    sessionmaker = get_sessionmaker()
    try:
        async with sessionmaker() as db_session:
            if decision == "approved":
                request = await approve_request(
                    db_session, request_id, operator=operator, params=None, reason=reason
                )
            else:
                request = await reject_request(
                    db_session, request_id, operator=operator, reason=reason
                )
            await db_session.commit()
        await publish_approval_event(
            tenant_id=operator.tenant_id,
            request=request,
            decision=decision,
            principal_sub=operator.sub,
            audit_id=request._audit_id,  # type: ignore[attr-defined]
        )
    except ApprovalNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="approval_request_not_found",
        ) from exc
    except UnauthorizedApprovalError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your role cannot decide approval requests (operator role required).",
        ) from exc
    except SelfApprovalForbiddenError as exc:
        # The disabled Approve button is UX only; a forged self-approve
        # lands here. Surface the break-glass hint the exception carries.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=("You cannot approve your own request (requester and approver must differ)."),
        ) from exc
    except ApprovalRequestAlreadyDecidedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This request was already {exc.status}.",
        ) from exc
    return request
