# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Audit-query forensic console: a filter form over unbounded history.

Initiative #1841 (G10.15 Audit-query forensic console), Task #1944 (T1).

The console already has a live activity feed (``/ui/broadcast``) and a
24h replay pane, but no **forensic** query surface: an operator could not
answer "who touched target X over all of history", "every write authorised
by ticket Y", or "show me audit row Z" from the console -- they reached for
the CLI. This surface is the entry chassis: a filter form, forward-cursor
paging, and one-click pivots to the pre-canned shortcuts (who-touched /
by-work-ref) and to the replay tree (T3).

Why a session BFF and not the Bearer ``/api/v1/audit/*`` routes
---------------------------------------------------------------

The REST audit routes (``api/v1/audit.py``) are Bearer-gated over a
verified JWT. A browser carrying only the BFF session cookie cannot
authenticate them. So this module dispatches the audit-query substrate
:func:`meho_backplane.audit_query.query_audit` **in-process** with
``tenant_id=session.tenant_id`` -- the same console-surface pattern the
approvals / corpus surfaces use. The in-process call avoids a self-HTTP hop
the cookie could not auth anyway, and ``tenant_id`` comes from the validated
session only, never a query parameter, so a tenant-A operator can never
surface tenant-B rows (the substrate's first WHERE clause is
``audit_log.tenant_id = :tenant_id``).

Two routes, one query
=====================

* ``GET /ui/audit`` -- the full page (``audit/index.html``, extends
  ``base.html``, sidebar highlight ``active_surface="audit"``): the filter
  form (target / principal / op_id / op_class dropdown / result_status /
  ``since`` / ``until`` duration text / work_ref) plus the first result
  page. Sets + echoes the CSRF cookie via :func:`mint_csrf_token` exactly
  as the broadcast feed does -- the form ``hx-get``s and the chassis
  convention pairs the cookie even though every route here is GET.
* ``GET /ui/audit/results`` -- the HTMX fragment swap target for the filter
  form **and** the forward-cursor pager. ``partial=rows`` (the "Load more"
  append fetch) returns ONLY the page's ``<li>`` rows plus an out-of-band
  pager re-render; any other value returns the full ``audit/_results.html``
  console block. No back button is rendered -- the substrate cursor is
  forward-only (:class:`AuditQueryResult`); paging back is "re-run from
  page 1".

Forward cursor vs the approvals offset pager
============================================

The approvals history list (``approvals/routes.py``) ships the same
"Load more" append affordance, but it pages via an **offset** + an
over-fetch-one ``has_more`` flag. This surface threads the substrate's
opaque **forward cursor** (``next_cursor``) instead: the rendered UX is
identical, the continuation token differs. The substrate itself already
over-fetches ``limit + 1`` to compute ``next_cursor``, so this surface
reads ``has_more`` straight off ``result.next_cursor is not None``.

Error mapping (mirrors the REST route, never a 500 on operator input)
====================================================================

* :class:`InvalidCursorError` -> reset to page 1. A tampered / expired
  cursor is treated as "start over" -- the query is re-run with no cursor
  rather than surfacing a 400/500.
* :class:`DurationParseError` -> inline field error on the ``since`` /
  ``until`` field; the first result page is suppressed.
* :class:`UnsupportedFilterError` -> inline error. (T1 exposes no filter
  that raises it -- ``parent_audit_id`` is not on the form -- but the
  mapping is wired for parity with the REST surface and future filters.)

Replay pivot RBAC (T3 deep-link)
================================

The replay pivot deep-links to the T3
``/ui/audit/sessions/{agent_session_id}/replay`` surface, which is
``TENANT_ADMIN``-gated (the REST replay route is ``tenant_admin`` per
#1844). It is rendered enabled only for a tenant admin; a plain operator
sees it disabled with a tooltip. The admin verdict is resolved via the
``runbooks/routes.py`` :func:`_resolve_role` fail-soft role lift (any
hiccup -> treat as operator), so an unavailable role lift degrades the
pivot to disabled rather than 5xx-ing the read surface.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.audit_query import (
    AuditQueryFilters,
    AuditQueryResult,
    DurationParseError,
    InvalidCursorError,
    UnsupportedFilterError,
    parse_duration,
    query_audit,
)
from meho_backplane.auth.jwt import verify_jwt_for_audience
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import classify_op
from meho_backplane.db.engine import get_raw_session, get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.auth.session_store import load_session
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.routes.broadcast.aggregate_gate import (
    INTERNAL_PAYLOAD_KEYS,
    fetch_audit_row,
    is_aggregate_only,
    resolve_op_id,
)
from meho_backplane.ui.routes.broadcast.feed import (
    OP_CLASS_BADGE_CLASSES,
    OP_CLASS_FILTER_OPTIONS,
)
from meho_backplane.ui.templating import get_templates

__all__ = ["build_audit_router"]

log = structlog.get_logger(__name__)

#: Page size for the result list. The forensic query is a browse surface,
#: not a glance: "Load more" threads the substrate's forward cursor to the
#: next page. Kept small so the first paint is fast over an unbounded
#: ``audit_log``; the substrate caps ``limit`` at 1000 regardless.
_PAGE_SIZE: Final[int] = 50

#: The ``result_status`` filter dropdown options. These are the four closed
#: values the substrate's ``_result_status_predicate`` understands (an
#: unknown value matches nothing rather than erroring); the empty default is
#: the "Any" sentinel that omits the filter. Surfaced in the
#: ok / pending / error / denied order an operator reads severity in.
_RESULT_STATUS_OPTIONS: Final[tuple[str, ...]] = ("ok", "pending", "error", "denied")

#: Max length accepted on the free-text filter inputs. Generous enough for
#: any real target name / principal sub / op_id glob / work_ref while keeping
#: the query string representable and out of unbounded-input territory --
#: mirrors the broadcast feed's per-field caps.
_MAX_FILTER_LENGTH: Final[int] = 256

#: Max length on the duration shorthand inputs (``since`` / ``until``).
#: Matches the REST route's ``max_length=32`` on the same fields.
_MAX_DURATION_LENGTH: Final[int] = 32

#: The ``partial`` discriminator on ``GET /ui/audit/results``. The empty
#: default returns the full ``audit/_results.html`` console block (the
#: form-submit swap target); ``rows`` returns ONLY the page's ``<li>`` rows
#: plus an out-of-band pager re-render -- the "Load more" append response. A
#: foreign value is rejected (422) rather than silently coerced (mirrors the
#: approvals history partial discipline).
_RESULTS_PARTIAL_ROWS: Final[str] = "rows"
_RESULTS_PARTIALS: Final[frozenset[str]] = frozenset({"", _RESULTS_PARTIAL_ROWS})

#: The ``since`` window for the my-recent quick view: the calling
#: operator's last-24h rows. Matches the REST ``/api/v1/audit/my-recent``
#: default (``audit.py:380``); my-recent is a glance ("what did I just
#: do"), not a forensic browse, so the window is fixed rather than a form
#: field.
_MY_RECENT_SINCE: Final[str] = "24h"

#: Module-level :class:`fastapi.Depends` closure for the operator-session
#: gate -- the ruff B008 idiom (no call in a default-argument position) the
#: broadcast / approvals / runbooks routes established.
_require_session = Depends(require_ui_session)

#: Module-level :class:`fastapi.Depends` for the raw-session drawer query
#: (same B008 guard) -- the drawer resolves one row directly, not through
#: the cursor-paged substrate.
_get_raw_session_dep = Depends(get_raw_session)


async def _resolve_role(session_ctx: UISessionContext) -> Operator | None:
    """Re-verify the session's access token to lift the operator's role.

    :class:`UISessionContext` carries ``operator_sub`` + ``tenant_id`` only,
    so the admin-vs-operator distinction the replay pivot needs is resolved
    by decrypting the stored access token and re-running the chassis JWT
    chain -- the same lift :func:`meho_backplane.ui.routes.runbooks.routes._resolve_role`
    performs.

    Fails **soft**: any hiccup (session row vanished between the middleware
    check and here, JWKS transiently unreachable, identity mismatch on the
    decoded token) returns ``None`` -- the caller then treats the request as
    a plain operator (the replay pivot renders disabled). An unavailable role
    lift must never 5xx the read surface.
    """
    try:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as db_session, db_session.begin():
            decrypted = await load_session(db_session, session_ctx.session_id)
        if decrypted is None:
            return None
        settings = get_settings()
        operator = await verify_jwt_for_audience(
            f"Bearer {decrypted.access_token}",
            expected_audience=settings.keycloak_audience,
        )
    except Exception as exc:
        log.info(
            "ui_audit_role_lift_unavailable",
            session_id=str(session_ctx.session_id),
            reason=type(exc).__name__,
        )
        return None
    # A token whose identity diverges from the session row is a security
    # anomaly; treat it as "no admin" rather than honouring the elevated
    # claim (the replay pivot stays disabled).
    if operator.sub != session_ctx.operator_sub or operator.tenant_id != session_ctx.tenant_id:
        log.warning(
            "ui_audit_role_lift_identity_mismatch",
            session_sub=session_ctx.operator_sub,
            token_sub=operator.sub,
        )
        return None
    return operator


async def _is_tenant_admin(session_ctx: UISessionContext) -> bool:
    """Resolve whether the session's operator is a ``tenant_admin``.

    Thin wrapper over :func:`_resolve_role` returning just the admin verdict
    the replay-pivot render needs. Fails soft to ``False`` (operator
    privileges) so the pivot is disabled whenever the role lift can't
    complete; the T3 replay route re-checks server-side, so a forged
    enabled pivot still 403s there.
    """
    operator = await _resolve_role(session_ctx)
    return operator is not None and operator.tenant_role is TenantRole.TENANT_ADMIN


def _filter_form_context(
    *,
    target: str,
    principal: str,
    op_id: str,
    op_class: str,
    result_status: str,
    since: str,
    until: str,
    work_ref: str,
) -> dict[str, object]:
    """Echo the operator's filter selection back into the form context.

    The same shape feeds the full page and the results fragment so the
    swapped-in fragment's form keeps the operator's values and the "Load
    more" continuation re-applies them. The op_class / result_status option
    lists ride along so the ``<select>``s render their closed vocabularies.
    """
    return {
        "op_class_options": OP_CLASS_FILTER_OPTIONS,
        "result_status_options": _RESULT_STATUS_OPTIONS,
        "target_filter": target,
        "principal_filter": principal,
        "op_id_filter": op_id,
        "op_class_filter": op_class,
        "result_status_filter": result_status,
        "since_filter": since,
        "until_filter": until,
        "work_ref_filter": work_ref,
    }


def _build_filters(
    *,
    target: str,
    principal: str,
    op_id: str,
    op_class: str,
    result_status: str,
    since: str,
    until: str,
    work_ref: str,
    cursor: str | None,
) -> AuditQueryFilters:
    """Construct the substrate filter object from the echoed form values.

    Blank inputs map to ``None`` (no filter) rather than an empty-string
    predicate. ``since`` / ``until`` are duration shorthand parsed at this
    router layer (the substrate takes :class:`datetime` only); a parse
    failure raises :class:`DurationParseError`, surfaced inline by the
    caller. ``tenant_id`` is **not** set here -- it is a mandatory keyword
    argument to :func:`query_audit`, injected from the session.
    """
    now = datetime.now(UTC)
    since_dt = parse_duration(since, now=now) if since else None
    until_dt = parse_duration(until, now=now) if until else None
    return AuditQueryFilters(
        target=target or None,
        principal=principal or None,
        op_id=op_id or None,
        op_class=op_class or None,
        result_status=result_status or None,
        since=since_dt,
        until=until_dt,
        work_ref=work_ref or None,
        limit=_PAGE_SIZE,
        cursor=cursor,
    )


async def _run_query(filters: AuditQueryFilters, *, tenant_id: uuid.UUID) -> AuditQueryResult:
    """Dispatch the audit-query substrate in-process, tenant-scoped.

    Acquires a session and calls :func:`query_audit` with the session's
    ``tenant_id`` as the mandatory keyword argument -- never a query
    parameter -- so cross-tenant rows are impossible by construction.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db_session:
        return await query_audit(filters, tenant_id=tenant_id, session=db_session)


def _badge_class(op_class: str) -> str:
    """Return the DaisyUI badge variant for *op_class* (broadcast palette).

    Reuses the broadcast :data:`OP_CLASS_BADGE_CLASSES` map so audit rows
    colour-code identically to live-feed rows; an op_class outside the closed
    vocabulary falls back to ``badge-ghost`` (the broadcast default).
    """
    return OP_CLASS_BADGE_CLASSES.get(op_class, "badge-ghost")


def _project_rows(result: AuditQueryResult, *, is_admin: bool) -> list[dict[str, object]]:
    """Project substrate :class:`AuditEntry` rows to the row template shape.

    Each projected row carries the colour-coded badge class, the three
    pivot affordances (who-touched on ``target_name``, by-work-ref on
    ``work_ref``, replay on ``agent_session_id``), and the admin verdict
    that gates the replay pivot's enabled state. ``params`` / raw payload are
    not projected -- this surface is the aggregate row list (the row detail
    drawer is T2).
    """
    return [
        {
            "id": str(row.id),
            "ts": row.ts.isoformat(),
            "principal_sub": row.principal_sub,
            "principal_name": row.principal_name,
            "target_name": row.target_name,
            "op_id": row.op_id,
            "op_class": row.op_class,
            "badge_class": _badge_class(row.op_class),
            "result_status": row.result_status,
            "work_ref": row.work_ref,
            "agent_session_id": (
                str(row.agent_session_id) if row.agent_session_id is not None else None
            ),
            "replay_enabled": is_admin and row.agent_session_id is not None,
        }
        for row in result.rows
    ]


def _build_drawer_context(row: AuditLog, *, is_admin: bool) -> dict[str, object]:
    """Assemble the row-detail drawer context for one ``audit_log`` row.

    Classifies the op via the same :func:`classify_op` chain the broadcast
    drawer uses, applies the shared aggregate-only gate
    (:func:`is_aggregate_only`) so a ``credential_read`` / ``credential_mint``
    / ``audit_query`` row -- or any row whose ``broadcast_detail_effective``
    is ``"aggregate"`` -- renders the 🔒 placeholder and **no** payload, and
    strips the audit-only classification + G6.3 forensic keys from the
    rendered request payload otherwise. The replay deep-link is enabled only
    when the session lifted to ``tenant_admin`` (``is_admin``) and the row
    carries an ``agent_session_id``; the parent-row deep-link re-opens the
    drawer on ``parent_audit_id``.
    """
    op_id = resolve_op_id(row)
    op_class = classify_op(op_id)
    aggregate_only = is_aggregate_only(row, op_class)
    # Only the full-detail path projects the request payload; the
    # aggregate-only branch never renders it at all (decision #3).
    request_payload = (
        {}
        if aggregate_only
        else {k: v for k, v in row.payload.items() if k not in INTERNAL_PAYLOAD_KEYS}
    )
    agent_session_id = str(row.agent_session_id) if row.agent_session_id is not None else None
    parent_audit_id = str(row.parent_audit_id) if row.parent_audit_id is not None else None
    return {
        "row": row,
        "op_id": op_id,
        "op_class": op_class,
        "badge_class": _badge_class(op_class),
        "aggregate_only": aggregate_only,
        "request_payload": request_payload,
        "agent_session_id": agent_session_id,
        "parent_audit_id": parent_audit_id,
        # The replay surface is TENANT_ADMIN-gated (#1844); the pivot is an
        # enabled deep-link only for an admin lift on a session-bearing row.
        "replay_enabled": is_admin and agent_session_id is not None,
    }


async def _resolve_deep_link_drawer(
    db_session: AsyncSession,
    *,
    session: UISessionContext,
    audit_id: uuid.UUID | None,
) -> dict[str, object] | None:
    """Resolve the ``?audit_id=`` page deep-link to a drawer context.

    Returns the drawer context to pre-render on initial page load, or
    ``None`` when no ``audit_id`` was supplied or it does not resolve in the
    operator's tenant. A missing / cross-tenant id degrades to "no open
    drawer" rather than 404-ing the whole page -- only the dedicated drawer
    fragment route (:func:`_drawer_handler`) returns the 404 not-found
    fragment. Tenant scoping is enforced by :func:`fetch_audit_row`.
    """
    if audit_id is None:
        return None
    row = await fetch_audit_row(db_session, tenant_id=session.tenant_id, audit_id=audit_id)
    if row is None:
        return None
    is_admin = await _is_tenant_admin(session)
    return _build_drawer_context(row, is_admin=is_admin)


async def _build_my_recent_context(session: UISessionContext) -> dict[str, object]:
    """Assemble the my-recent quick-view context (operator-self-scoped).

    Binds ``principal=session.operator_sub`` so the query returns only the
    calling operator's own rows -- a second operator's activity is never
    surfaced through this route (mirrors the REST ``/api/v1/audit/my-recent``
    ``principal=operator.sub`` binding, ``audit.py:380``). Reuses the T1 row
    projection so each row renders through the shared row partial and opens
    the same detail drawer. A bad cursor / duration is impossible here (the
    window is the fixed ``_MY_RECENT_SINCE`` shorthand, no operator input).
    """
    is_admin = await _is_tenant_admin(session)
    filters = _build_filters(
        target="",
        principal=session.operator_sub,
        op_id="",
        op_class="",
        result_status="",
        since=_MY_RECENT_SINCE,
        until="",
        work_ref="",
        cursor=None,
    )
    result = await _run_query(filters, tenant_id=session.tenant_id)
    return {
        "rows": _project_rows(result, is_admin=is_admin),
        "operator_sub": session.operator_sub,
    }


async def _build_results_context(
    session: UISessionContext,
    *,
    target: str,
    principal: str,
    op_id: str,
    op_class: str,
    result_status: str,
    since: str,
    until: str,
    work_ref: str,
    cursor: str | None,
) -> dict[str, object]:
    """Assemble the context shared by the full page + the results fragment.

    Runs the query, projects the rows, and threads the forward cursor +
    duration / cursor / filter error states. A tampered cursor resets to
    page 1 (re-run with no cursor) rather than erroring; a bad duration
    surfaces an inline field error and suppresses the rows.
    """
    is_admin = await _is_tenant_admin(session)
    context: dict[str, object] = {
        **_filter_form_context(
            target=target,
            principal=principal,
            op_id=op_id,
            op_class=op_class,
            result_status=result_status,
            since=since,
            until=until,
            work_ref=work_ref,
        ),
        "rows": [],
        "next_cursor": None,
        "has_more": False,
        "duration_error": None,
        "filter_error": None,
        "page_size": _PAGE_SIZE,
    }

    try:
        filters = _build_filters(
            target=target,
            principal=principal,
            op_id=op_id,
            op_class=op_class,
            result_status=result_status,
            since=since,
            until=until,
            work_ref=work_ref,
            cursor=cursor,
        )
    except DurationParseError as exc:
        # Inline field error on since/until -- never a 500 on operator input.
        context["duration_error"] = str(exc)
        return context

    try:
        result = await _run_query(filters, tenant_id=session.tenant_id)
    except InvalidCursorError:
        # A tampered / expired cursor is "start over": re-run from page 1
        # with no cursor rather than surfacing a 400/500 to the operator.
        log.info("ui_audit_invalid_cursor_reset", session_id=str(session.session_id))
        filters = _build_filters(
            target=target,
            principal=principal,
            op_id=op_id,
            op_class=op_class,
            result_status=result_status,
            since=since,
            until=until,
            work_ref=work_ref,
            cursor=None,
        )
        result = await _run_query(filters, tenant_id=session.tenant_id)
    except UnsupportedFilterError as exc:
        # No T1 filter raises this (parent_audit_id is not on the form), but
        # map it inline for parity with the REST route and future filters.
        context["filter_error"] = str(exc)
        return context

    context["rows"] = _project_rows(result, is_admin=is_admin)
    context["next_cursor"] = result.next_cursor
    context["has_more"] = result.next_cursor is not None
    return context


async def _results_handler(
    request: Request,
    target: str = Query(default="", max_length=_MAX_FILTER_LENGTH),
    principal: str = Query(default="", max_length=_MAX_FILTER_LENGTH),
    op_id: str = Query(default="", max_length=_MAX_FILTER_LENGTH),
    op_class: str = Query(default="", max_length=64),
    result_status: str = Query(default="", max_length=16),
    since: str = Query(default="", max_length=_MAX_DURATION_LENGTH),
    until: str = Query(default="", max_length=_MAX_DURATION_LENGTH),
    work_ref: str = Query(default="", max_length=_MAX_FILTER_LENGTH),
    cursor: str | None = Query(default=None),
    partial: str = Query(default=""),
    session: UISessionContext = _require_session,
) -> HTMLResponse:
    """``GET /ui/audit/results`` -- the filter-submit + pager fragment.

    ``partial=rows`` (the "Load more" append fetch) renders ONLY the
    page's rows plus an out-of-band pager re-render; any other value
    renders the full ``audit/_results.html`` console block. A foreign
    ``partial`` value is rejected (422) rather than silently coerced.
    """
    if partial not in _RESULTS_PARTIALS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unknown audit results partial '{partial}'.",
        )
    context = await _build_results_context(
        session,
        target=target,
        principal=principal,
        op_id=op_id,
        op_class=op_class,
        result_status=result_status,
        since=since,
        until=until,
        work_ref=work_ref,
        cursor=cursor,
    )
    template = (
        "audit/_results_rows_oob.html"
        if partial == _RESULTS_PARTIAL_ROWS
        else "audit/_results.html"
    )
    return get_templates().TemplateResponse(request, template, context)


async def _page_handler(
    request: Request,
    target: str = Query(default="", max_length=_MAX_FILTER_LENGTH),
    principal: str = Query(default="", max_length=_MAX_FILTER_LENGTH),
    op_id: str = Query(default="", max_length=_MAX_FILTER_LENGTH),
    op_class: str = Query(default="", max_length=64),
    result_status: str = Query(default="", max_length=16),
    since: str = Query(default="", max_length=_MAX_DURATION_LENGTH),
    until: str = Query(default="", max_length=_MAX_DURATION_LENGTH),
    work_ref: str = Query(default="", max_length=_MAX_FILTER_LENGTH),
    audit_id: uuid.UUID | None = Query(default=None),
    session: UISessionContext = _require_session,
    db_session: AsyncSession = _get_raw_session_dep,
) -> HTMLResponse:
    """``GET /ui/audit`` -- the full page: filter form + first result page.

    Filters are accepted on the full-page route too so a copy-pasted
    filtered URL reproduces the operator's view (the form sets
    ``hx-push-url`` on the fragment route, mirroring broadcast /
    operations). Sets + echoes the CSRF cookie so the form's ``hx-get``
    passes the double-submit check, even though every route here is GET.

    ``?audit_id=<id>`` is the drawer deep-link: pasting an audit id into
    a ticket opens the page with that row's detail drawer already
    rendered in the drawer slot. A missing / cross-tenant id resolves to
    ``None`` and simply renders the page with no open drawer (the page
    itself is not a 404 -- only the drawer fragment is).
    """
    context = await _build_results_context(
        session,
        target=target,
        principal=principal,
        op_id=op_id,
        op_class=op_class,
        result_status=result_status,
        since=since,
        until=until,
        work_ref=work_ref,
        cursor=None,
    )
    csrf_token = mint_csrf_token(str(session.session_id))
    context["page_title"] = "Audit"
    context["active_surface"] = "audit"
    context["csrf_token"] = csrf_token
    context["drawer"] = await _resolve_deep_link_drawer(
        db_session, session=session, audit_id=audit_id
    )
    response = get_templates().TemplateResponse(request, "audit/index.html", context)
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )
    return response


async def _my_recent_handler(
    request: Request,
    session: UISessionContext = _require_session,
) -> HTMLResponse:
    """``GET /ui/audit/my-recent`` -- the my-recent quick-view fragment.

    Returns the calling operator's last-24h rows
    (``principal=session.operator_sub``) rendered through the same row
    partial as the T1 result list, so each row opens the T2 detail
    drawer. ``OPERATOR``-self-scoped: a second operator's activity is
    never reachable here. HTMX-only fragment (no full-page chrome).
    """
    context = await _build_my_recent_context(session)
    return get_templates().TemplateResponse(request, "audit/_my_recent.html", context)


async def _drawer_handler(
    request: Request,
    audit_id: uuid.UUID,
    session: UISessionContext = _require_session,
    db_session: AsyncSession = _get_raw_session_dep,
) -> HTMLResponse:
    """``GET /ui/audit/show/{audit_id}`` -- the row detail drawer fragment.

    Resolves the row in-process scoped to ``session.tenant_id`` (the
    shared :func:`fetch_audit_row`); a missing / cross-tenant id renders
    the not-found fragment at **404** (never 403, never 200), matching
    the REST ``show`` non-leakage posture (``audit.py:399-404``) and the
    broadcast drawer. The aggregate-only gate withholds the payload for
    sensitive ops; the replay deep-link is enabled only for a
    ``tenant_admin`` lift.
    """
    row = await fetch_audit_row(
        db_session,
        tenant_id=session.tenant_id,
        audit_id=audit_id,
    )
    if row is None:
        return get_templates().TemplateResponse(
            request,
            "audit/_drawer_not_found.html",
            {"audit_id": str(audit_id)},
            status_code=404,
        )
    is_admin = await _is_tenant_admin(session)
    context = _build_drawer_context(row, is_admin=is_admin)
    return get_templates().TemplateResponse(request, "audit/_drawer.html", context)


def build_audit_router() -> APIRouter:
    """Construct the ``/ui/audit*`` :class:`APIRouter`.

    Factory function (not a module-level constant) so a test app can
    construct parallel routers without shared route state -- the convention
    every surface router (broadcast / approvals / runbooks) follows.
    Registered ahead of the stubs aggregate in
    :func:`meho_backplane.ui.routes.build_router`.

    Route ordering is the first-match-wins contract. The **literal**
    segments are registered before the ``{audit_id}`` parametrised drawer
    so a literal path never binds as a slug:

    1. ``GET /ui/audit/results`` -- the T1 filter-submit + pager fragment.
    2. ``GET /ui/audit/my-recent`` -- the my-recent quick view (literal,
       registered **before** ``show/{audit_id}`` so ``my-recent`` is never
       read as an ``audit_id``).
    3. ``GET /ui/audit/show/{audit_id}`` -- the T2 row detail drawer.
    4. ``GET /ui/audit`` -- the full page.

    A future T3 ``/ui/audit/sessions/{session_id}/replay`` slots in among the
    literal-prefixed routes the same way.
    """
    router = APIRouter(tags=["ui-audit"])
    router.add_api_route(
        "/ui/audit/results",
        _results_handler,
        methods=["GET"],
        name="ui_audit_results",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/audit/my-recent",
        _my_recent_handler,
        methods=["GET"],
        name="ui_audit_my_recent",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/audit/show/{audit_id}",
        _drawer_handler,
        methods=["GET"],
        name="ui_audit_drawer",
        response_class=HTMLResponse,
        responses={
            404: {
                "description": (
                    "Audit id does not exist in this tenant (or exists only "
                    "for another tenant). Returns the not-found drawer fragment."
                ),
                "content": {"text/html": {}},
            },
        },
    )
    router.add_api_route(
        "/ui/audit",
        _page_handler,
        methods=["GET"],
        name="ui_audit_page",
        response_class=HTMLResponse,
    )
    return router
