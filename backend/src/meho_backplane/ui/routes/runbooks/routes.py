# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Runbooks UI read surface: catalog browse + template detail.

Initiative #1381 (G10.6 Runbooks UI), Task #1382 (T1). Three routes
mounted under ``/ui/runbooks*`` that surface the G12.2 runbook template
layer (REST: :mod:`meho_backplane.api.v1.runbook_templates`; service:
:class:`~meho_backplane.runbooks.service.RunbookTemplateService`) to
operators on the console. Authoring (T2) and lifecycle controls (T3)
follow in sibling tasks; this surface is read-only.

Route inventory
---------------

* ``GET /ui/runbooks`` -- catalog page. Lists the latest version of each
  template slug for the operator's tenant (slug / version / title /
  status / target_kind / edited_at) as DaisyUI rows with status badges.
  ``status`` (draft / published / deprecated) and ``target_kind`` filters
  drive an HTMX partial swap. ``HX-Request: true`` returns only the
  ``runbooks/_list.html`` fragment; a direct navigation returns the full
  page.

* ``GET /ui/runbooks/list`` -- HTMX filter partial. Same projection as the
  catalog, parameterised by the ``status`` / ``target_kind`` query params
  the filter controls carry. Registered **before** ``/ui/runbooks/{slug}``
  so FastAPI's first-match-wins routing does not swallow the literal
  ``list`` segment as a slug parameter.

* ``GET /ui/runbooks/{slug}`` -- template detail. Consumes
  :meth:`~meho_backplane.runbooks.service.RunbookTemplateService.show_template`
  and renders the title / description / target_kind / status plus the
  ordered steps (``manual`` vs ``operation_call``, showing op_id / params)
  and verify gates (``confirm`` prompt vs ``operation_call`` op_id /
  params / expect). Each step ``body`` is rendered server-side via the KB
  Markdown renderer :func:`~meho_backplane.ui.routes.kb.render.render_markdown`.

Opacity floor
-------------

The G12.3-T4 (#1309) carve-out the REST ``show`` route enforces is
mirrored here. A ``tenant_admin`` always sees the full steps. An
``operator`` sees the full steps only when they have a ``completed`` or
``abandoned`` run against the resolved ``(slug, version)`` -- the
:meth:`~meho_backplane.runbooks.run_service.RunbookRunService.can_show_template_post_completion`
predicate. An operator with no such run (or only an in-flight run) is
*not* shown a raw 403: the detail view renders the catalog-level summary
(title / description / target_kind / status / step count) plus a clear
"step details are restricted until you complete a run of this template"
notice. This matches the REST surface's ``403 detail="opacity_floor"``
posture while keeping the console a navigable page rather than an error.

The role lift fails **soft** to operator-level privileges (no admin) when
the JWT round-trip can't complete (session row vanished mid-request, JWKS
transiently unreachable) -- the same posture
:func:`meho_backplane.ui.routes.connectors.operator.resolve_role_probe`
adopts. The restricted-detail render is the safe default, never a 5xx.

Tenant scoping
--------------

Every handler derives tenant identity from
:class:`~meho_backplane.ui.auth.middleware.UISessionContext`. No query
parameter or form field overrides tenant; a cross-tenant slug probe is
invisible to the service's tenant filter and surfaces as the restricted
state (operator) or 404 (admin) -- the same anti-enumeration posture the
``/api/v1/runbooks/templates`` surface uses.

RBAC
----

Both routes require ``operator`` minimum, enforced by
:func:`~meho_backplane.ui.auth.middleware.require_ui_session`. The
admin-vs-operator distinction the opacity-floor branch needs is resolved
by re-verifying the session's access token (see :func:`_resolve_role`),
not carried on the session row.

References
----------

* HTMX 2.0.9 debounced filtering + ``HX-Request`` fragment swap:
  https://htmx.org/attributes/hx-trigger/
* DaisyUI 5.5.20 badges / cards / tables: https://daisyui.com/components/badge/
* markdown-it-py 4.2.0 (shared KB renderer): https://markdown-it-py.readthedocs.io/
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from meho_backplane.auth.jwt import verify_jwt_for_audience
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.runbooks.run_service import RunbookRunService
from meho_backplane.runbooks.schemas import (
    ListTemplatesFilter,
    ShowTemplateResponse,
    TemplateSummary,
)
from meho_backplane.runbooks.service import (
    RunbookTemplateService,
    TemplateNotFoundError,
)
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.auth.session_store import load_session
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.routes.kb.render import pygments_css, render_markdown
from meho_backplane.ui.routes.runbooks.driver import register_driver_routes
from meho_backplane.ui.routes.runbooks.editor_routes import register_editor_routes
from meho_backplane.ui.routes.runbooks.lifecycle import register_lifecycle_routes
from meho_backplane.ui.routes.runbooks.runs import register_runs_routes
from meho_backplane.ui.templating import get_templates

__all__ = ["build_runbooks_router"]

log = structlog.get_logger(__name__)

#: The closed status vocabulary the list filter accepts. An out-of-vocab
#: value trips a clean 422 at FastAPI's query-parameter boundary (matching
#: the ``/api/v1/runbooks/templates`` list route's ``Literal`` typing)
#: rather than constructing an invalid :class:`ListTemplatesFilter`.
_StatusFilter = Literal["draft", "published", "deprecated"]

#: Default + cap on the number of catalog rows surfaced. The REST list
#: route caps at 500; the console page mirrors that ceiling so a tenant
#: with a large template library still renders deterministically.
_DEFAULT_LIMIT: Final[int] = 100
_MAX_LIMIT: Final[int] = 500

#: Cap on the free-text ``target_kind`` filter value. Generous enough for
#: any real connector/resource kind label while keeping the query string
#: representable and out of unbounded-input territory.
_MAX_TARGET_KIND_LENGTH: Final[int] = 128

#: Module-level ``Depends`` closure for the operator-session gate. Matches
#: the B008 idiom the kb / dashboard routes use (no call in a default arg).
_require_session = Depends(require_ui_session)


@dataclass(frozen=True)
class _DetailView:
    """Resolved detail payload for one template + its opacity-floor verdict.

    ``restricted`` is ``True`` when the caller is an operator without a
    completed/abandoned run against ``(slug, version)`` -- the template
    summary still renders, but :attr:`steps_rendered` is empty and the
    template shows the restricted-detail notice. ``False`` means the full
    steps are visible (admin, or post-completion operator).
    """

    template: ShowTemplateResponse
    restricted: bool
    steps_rendered: list[dict[str, object]]


async def _resolve_role(session_ctx: UISessionContext) -> Operator | None:
    """Re-verify the session's access token to lift the operator's role.

    :class:`UISessionContext` carries ``operator_sub`` + ``tenant_id`` only,
    so the admin-vs-operator distinction the opacity floor needs is resolved
    by decrypting the stored access token and re-running the chassis JWT
    chain (the same lift
    :mod:`meho_backplane.ui.routes.connectors.operator` performs).

    Fails **soft**: any hiccup (session row vanished between the middleware
    check and here, JWKS transiently unreachable, identity mismatch on the
    decoded token) returns ``None`` -- the caller then treats the request as
    a plain operator (no admin privilege). The opacity-floor branch is the
    safe default; an unavailable role lift must never 5xx the read surface.
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
            "ui_runbooks_role_lift_unavailable",
            session_id=str(session_ctx.session_id),
            reason=type(exc).__name__,
        )
        return None
    # A token whose identity diverges from the session row is a security
    # anomaly; treat it as "no admin" rather than honouring the elevated
    # claim. The write surfaces (T2/T3) will gate hard; this read surface
    # degrades to the restricted view.
    if operator.sub != session_ctx.operator_sub or operator.tenant_id != session_ctx.tenant_id:
        log.warning(
            "ui_runbooks_role_lift_identity_mismatch",
            session_sub=session_ctx.operator_sub,
            token_sub=operator.sub,
        )
        return None
    return operator


async def _is_admin(session_ctx: UISessionContext) -> bool:
    """Resolve whether the session's operator is a ``tenant_admin``.

    Thin wrapper over :func:`_resolve_role` returning just the admin verdict --
    the catalog + list-fragment renders need the boolean (to show / hide the
    lifecycle row actions) but not the full :class:`Operator`. Fails soft to
    ``False`` (operator privileges) via :func:`_resolve_role`, so the row
    actions are hidden whenever the role lift can't complete. The POST handlers
    re-check server-side, so a hidden-but-forged action is still a 403.
    """
    operator = await _resolve_role(session_ctx)
    return operator is not None and operator.tenant_role == TenantRole.TENANT_ADMIN


def _render_steps(template: ShowTemplateResponse) -> list[dict[str, object]]:
    """Project the template's ordered steps into render-ready dicts.

    Each entry carries the raw step (for type / op_id / params access in
    the template) plus the pre-rendered Markdown ``body_html`` so the
    Jinja layer never calls the renderer itself. Both step kinds
    (``manual`` / ``operation_call``) and both verify kinds (``confirm`` /
    ``operation_call``) are surfaced; the template branches on the
    ``.type`` discriminators.
    """
    return [
        {
            "step": step,
            "body_html": render_markdown(step.body),
        }
        for step in template.steps
    ]


def _set_csrf_cookie(response: HTMLResponse, csrf_token: str) -> None:
    """Set the JS-readable CSRF cookie on *response* (shared posture).

    Mirrors the kb / dashboard surfaces: not ``HttpOnly`` (HTMX must read
    it to echo ``X-CSRF-Token`` on any future state-changing request),
    ``Secure`` + ``SameSite=Strict``, scoped to ``/ui``.
    """
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )


async def _list_summaries(
    tenant_id: object,
    status: _StatusFilter | None,
    target_kind: str | None,
) -> list[TemplateSummary]:
    """Fetch the catalog projection for the current tenant + filters."""
    template_filter = ListTemplatesFilter(status=status, target_kind=target_kind)
    return await RunbookTemplateService().list_templates(
        tenant_id,  # type: ignore[arg-type]
        template_filter,
        limit=_DEFAULT_LIMIT,
    )


async def _render_index(
    request: Request,
    session: UISessionContext,
    status: _StatusFilter | None,
    target_kind: str | None,
) -> HTMLResponse:
    """Render the catalog page, or the HTMX ``_list.html`` fragment.

    ``HX-Request: true`` -> the fragment only (filter swaps ``#runbooks-list``
    in place). Direct navigation -> the full page. Both share the same
    projection so the markup is identical.
    """
    summaries = await _list_summaries(session.tenant_id, status, target_kind)
    csrf_token = mint_csrf_token(str(session.session_id))
    is_admin = await _is_admin(session)
    context = {
        "summaries": summaries,
        "status_filter": status or "",
        "target_kind_filter": target_kind or "",
        "is_admin": is_admin,
        "operator_sub": session.operator_sub,
        "csrf_token": csrf_token,
        "active_surface": "runbooks",
        "page_title": "Runbooks",
    }
    if request.headers.get("HX-Request") == "true":
        return get_templates().TemplateResponse(request, "runbooks/_list.html", context)
    response = get_templates().TemplateResponse(request, "runbooks/index.html", context)
    _set_csrf_cookie(response, csrf_token)
    return response


async def _render_list_fragment(
    request: Request,
    session: UISessionContext,
    status: _StatusFilter | None,
    target_kind: str | None,
) -> HTMLResponse:
    """Render the ``_list.html`` fragment for the HTMX filter controls.

    The fragment re-mints the CSRF token into the admin row-action
    Publish/Deprecate buttons (``hx-headers``), so the response **must** also
    refresh the ``meho_csrf`` cookie to match -- otherwise the next row-action
    POST presents a header token that no longer equals the stale cookie and the
    :class:`~meho_backplane.ui.csrf.CSRFMiddleware` ``value_mismatch`` check
    403s the action. Filter swaps and the post-action ``runbooks-refresh``
    reload both re-render via this path, so a missing cookie refresh breaks the
    catalog row action on the second interaction. Mirrors the cookie posture of
    :func:`_render_index` / :func:`_render_detail` (mint -> ``_set_csrf_cookie``).
    """
    summaries = await _list_summaries(session.tenant_id, status, target_kind)
    csrf_token = mint_csrf_token(str(session.session_id))
    is_admin = await _is_admin(session)
    context = {
        "summaries": summaries,
        "status_filter": status or "",
        "target_kind_filter": target_kind or "",
        "is_admin": is_admin,
        "csrf_token": csrf_token,
        "active_surface": "runbooks",
    }
    response = get_templates().TemplateResponse(request, "runbooks/_list.html", context)
    _set_csrf_cookie(response, csrf_token)
    return response


async def _render_detail(
    request: Request,
    session: UISessionContext,
    slug: str,
    version: int | None,
) -> HTMLResponse:
    """Render the opacity-floor-aware template detail page.

    A ``tenant_admin`` always sees full steps. An ``operator`` sees full
    steps only with a completed/abandoned run against the resolved
    ``(slug, version)``; otherwise the summary + restricted-detail notice
    renders (never a raw 403). A missing slug 404s for an admin and
    collapses to the restricted state for an operator -- the same
    anti-enumeration posture the REST ``show`` route uses.
    """
    operator = await _resolve_role(session)
    is_admin = operator is not None and operator.tenant_role == TenantRole.TENANT_ADMIN
    detail = await _resolve_detail(session, slug, version, is_admin=is_admin)
    csrf_token = mint_csrf_token(str(session.session_id))
    # The fork-on-edit affordance surfaces how many runs are still pinned to a
    # published version (forking leaves them bound to the source). Only a real,
    # admin-visible published row needs the count; the restricted placeholder
    # and non-published states show none.
    in_flight_run_count = 0
    if not detail.restricted and detail.template.status == "published":
        in_flight_run_count = await RunbookTemplateService().count_in_flight_runs(
            session.tenant_id, detail.template.slug, detail.template.version
        )
    context = {
        "template": detail.template,
        "restricted": detail.restricted,
        "steps_rendered": detail.steps_rendered,
        "code_css": pygments_css(),
        "is_admin": is_admin,
        "in_flight_run_count": in_flight_run_count,
        "lifecycle_error": None,
        "swap_badge": False,
        "operator_sub": session.operator_sub,
        "csrf_token": csrf_token,
        "active_surface": "runbooks",
        "page_title": f"{detail.template.slug} · Runbooks",
    }
    response = get_templates().TemplateResponse(request, "runbooks/detail.html", context)
    _set_csrf_cookie(response, csrf_token)
    return response


def build_runbooks_router() -> APIRouter:
    """Construct the ``/ui/runbooks*`` :class:`APIRouter`.

    Factory function (not a module-level constant) so a test app can
    construct parallel routers without shared route state -- same
    convention as :func:`meho_backplane.ui.routes.kb.build_kb_router`.
    The handler bodies live in module-level ``_render_*`` helpers; the
    factory is thin registration.

    ``/ui/runbooks/list`` is registered **before** ``/ui/runbooks/{slug}``
    so FastAPI's first-match-wins routing does not bind the literal
    ``list`` segment to the slug path parameter.
    """
    router = APIRouter(tags=["ui-runbooks"])

    @router.get("/ui/runbooks", response_class=HTMLResponse)
    async def runbooks_index(
        request: Request,
        session: UISessionContext = _require_session,
        status: _StatusFilter | None = Query(default=None),
        target_kind: str | None = Query(default=None, max_length=_MAX_TARGET_KIND_LENGTH),
    ) -> HTMLResponse:
        return await _render_index(request, session, status, target_kind)

    @router.get("/ui/runbooks/list", response_class=HTMLResponse)
    async def runbooks_list(
        request: Request,
        session: UISessionContext = _require_session,
        status: _StatusFilter | None = Query(default=None),
        target_kind: str | None = Query(default=None, max_length=_MAX_TARGET_KIND_LENGTH),
    ) -> HTMLResponse:
        return await _render_list_fragment(request, session, status, target_kind)

    # The T2 (#1383) authoring routes (``/ui/runbooks/new`` GET/POST +
    # ``/ui/runbooks/preview`` POST + ``/ui/runbooks/{slug}/edit`` GET/POST)
    # are registered here -- BEFORE ``/ui/runbooks/{slug}`` so FastAPI's
    # first-match-wins routing does not swallow the literal ``new`` /
    # ``preview`` segments as slug parameters. Their handlers + the form
    # (de)serialisation live in ``runbooks.editor``.
    register_editor_routes(router)

    # The T3 (#1384) lifecycle routes (``/ui/runbooks/{slug}/publish`` +
    # ``/ui/runbooks/{slug}/deprecate`` POST) -- registered here, also BEFORE
    # ``/ui/runbooks/{slug}`` so the literal ``publish`` / ``deprecate`` tail
    # segments are not swallowed by the slug catch-all.
    register_lifecycle_routes(router)

    # The #1837-T1 (#1884) run surface (``GET /ui/runbooks/runs`` list,
    # ``GET /ui/runbooks/runs/start`` modal, ``POST /ui/runbooks/runs`` start)
    # -- registered here, BEFORE ``/ui/runbooks/{slug}`` so the literal
    # ``runs`` segment is not bound as a slug parameter, and ``runs/start``
    # ahead of the ``runs/{run_id}`` driver (T2 #1893, below). Their handlers
    # live in ``runbooks.runs``.
    register_runs_routes(router)

    # The #1837-T2 (#1893) run *driver* (``GET /ui/runbooks/runs/{run_id}`` +
    # ``POST .../next|abort|reassign``) -- registered here, AFTER
    # ``register_runs_routes`` so T1's literal ``/ui/runbooks/runs/start`` is
    # matched before the ``{run_id}`` param route (first-match-wins, else
    # ``start`` would bind as a ``run_id``), and BEFORE ``/ui/runbooks/{slug}``
    # so ``runs`` is not swallowed as a slug. Their handlers + the opacity-safe
    # single-step render live in ``runbooks.driver``.
    register_driver_routes(router)

    @router.get("/ui/runbooks/{slug}", response_class=HTMLResponse)
    async def runbooks_detail(
        slug: str,
        request: Request,
        session: UISessionContext = _require_session,
        version: int | None = Query(default=None, ge=1),
    ) -> HTMLResponse:
        return await _render_detail(request, session, slug, version)

    return router


async def _resolve_detail(
    session: UISessionContext,
    slug: str,
    version: int | None,
    *,
    is_admin: bool,
) -> _DetailView:
    """Resolve the template + its opacity-floor verdict for the detail view.

    Dispatches to the admin or operator path. The admin path always shows
    full steps (missing row -> 404). The operator path applies the
    post-completion predicate and degrades to the restricted state rather
    than leaking slug existence via a status-code differential.
    """
    templates_service = RunbookTemplateService()
    if is_admin:
        return await _resolve_detail_admin(templates_service, session, slug, version)
    return await _resolve_detail_operator(
        templates_service, RunbookRunService(), session, slug, version
    )


async def _resolve_detail_admin(
    templates_service: RunbookTemplateService,
    session: UISessionContext,
    slug: str,
    version: int | None,
) -> _DetailView:
    """Admin path: full steps; a missing/cross-tenant row 404s.

    The service's tenant filter makes another tenant's row invisible, so a
    cross-tenant probe raises :class:`TemplateNotFoundError` exactly as a
    genuinely-absent slug does -- both collapse to 404.
    """
    try:
        template = await templates_service.show_template(session.tenant_id, slug, version=version)
    except TemplateNotFoundError as exc:
        raise HTTPException(status_code=404, detail="runbook_template_not_found") from exc
    return _DetailView(template=template, restricted=False, steps_rendered=_render_steps(template))


async def _resolve_detail_operator(
    templates_service: RunbookTemplateService,
    run_service: RunbookRunService,
    session: UISessionContext,
    slug: str,
    version: int | None,
) -> _DetailView:
    """Operator path: opacity-floor-gated.

    Resolve the row first (so the predicate checks the same version the
    page would show). A missing/cross-tenant slug becomes a synthetic
    restricted view so existence never leaks (mirrors the REST
    ``opacity_floor`` 403). When the row resolves, the post-completion
    predicate unlocks the full steps; otherwise the summary renders with
    the restricted notice and the steps are withheld.
    """
    try:
        template = await templates_service.show_template(session.tenant_id, slug, version=version)
    except TemplateNotFoundError:
        return _DetailView(
            template=_restricted_placeholder(slug, version),
            restricted=True,
            steps_rendered=[],
        )
    unlocked = await run_service.can_show_template_post_completion(
        session.tenant_id,
        session.operator_sub,
        slug,
        template.version,
    )
    if unlocked:
        return _DetailView(
            template=template, restricted=False, steps_rendered=_render_steps(template)
        )
    return _DetailView(template=template, restricted=True, steps_rendered=[])


def _restricted_placeholder(slug: str, version: int | None) -> ShowTemplateResponse:
    """Build a minimal summary for an operator probing an unresolvable slug.

    The detail view still renders a page (never a 404 for operators), but
    no real template metadata leaks -- only the slug the operator typed and
    the restricted notice. Timestamps are epoch sentinels; the template
    never renders them in the restricted branch.
    """
    from datetime import UTC, datetime

    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    return ShowTemplateResponse(
        slug=slug,
        version=version or 0,
        title=slug,
        description="",
        target_kind=None,
        status="draft",
        steps=[],
        created_by="",
        created_at=epoch,
        edited_by="",
        edited_at=epoch,
    )
