# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Conventions UI route registration -- maps HTTP verbs to render helpers.

Initiative #1838 (G10.12 Conventions console), Task #1895 (T1). The
route handlers are thin wrappers: parse FastAPI params, resolve the
session + read-context dependency, and hand off to the render functions
in :mod:`~meho_backplane.ui.routes.conventions.views`.

Route inventory (T1 #1895 -- read surface):

* ``GET /ui/conventions`` -- list page or HTMX table fragment.
  ``?kind=operational|workflow|reference`` filters the table; the
  always-on token-budget banner always reflects the full operational
  set.
* ``GET /ui/conventions/{slug}`` -- detail page or HTMX body fragment:
  full ``body`` rendered via the sanitised ``render_markdown`` helper.

Route registration order is **load-bearing**. T2 (#1838-T2) will add the
static-prefix write routes ``/ui/conventions/create`` + ``/ui/conventions/preview``;
those MUST register before the parametrised ``/ui/conventions/{slug}``
because FastAPI matches the first route whose path template fits, and
``{slug}`` would otherwise consume the literal ``"create"`` /
``"preview"`` token. This Task already registers the literal
``/ui/conventions`` ahead of ``/ui/conventions/{slug}`` and splits the
registration into a static-prefix helper + a parametrised helper so the
T2 routes drop into the static group without re-litigating the
ordering. The same static-before-param discipline the memory surface
holds (``memory/routes.py``).

RBAC: read = OPERATOR (mirrors the REST ``GET /api/v1/conventions``
``require_role(OPERATOR)`` gate and the runbooks read/write split). The
read context's ``is_tenant_admin`` flag is a UX hint that gates the T2
author / edit / delete affordances in the template; the write routes
T2 adds re-check role server-side.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_raw_session, get_session
from meho_backplane.ui.routes.conventions.history import render_history_panel
from meho_backplane.ui.routes.conventions.operator import (
    ConventionsReadContext,
    ConventionsWriteContext,
    resolve_read_context,
    resolve_write_context,
)
from meho_backplane.ui.routes.conventions.views import (
    KIND_ALL,
    render_detail,
    render_index,
    validate_slug,
)
from meho_backplane.ui.routes.conventions.write import (
    BODY_MAX_LENGTH,
    PRIORITY_MAX,
    PRIORITY_MIN,
    SLUG_MAX_LENGTH,
    TITLE_MAX_LENGTH,
    delete_convention,
    render_create_modal,
    render_delete_confirm,
    render_edit_modal,
    render_token_preview,
    submit_create,
    submit_update,
)

__all__ = ["build_conventions_router"]

#: Module-level :class:`Depends` closures -- ruff B008 idiom mirroring
#: the memory + topology routes (no calls in default argument positions).
_session_dep = Depends(get_session)
#: Write + modal-render handlers thread a NON-transactional session
#: (no outer ``begin()``) so the service flush + the handler's explicit
#: ``commit()`` / ``rollback()`` own the transaction. This is what lets a
#: 409 / 422 roll the failed flush back and still return an inline-error
#: 200 fragment -- the request-scoped :func:`get_session` would otherwise
#: try to commit the dirty (or already-rolled-back) transaction on a
#: clean handler return. Mirrors the G9.2 curated-edge service-owned-
#: transaction routes' use of :func:`get_raw_session`.
_raw_session_dep = Depends(get_raw_session)
_read_ctx_dep = Depends(resolve_read_context)
_write_ctx_dep = Depends(resolve_write_context)


async def _list_handler(
    request: Request,
    kind: str = Query(default=KIND_ALL, max_length=32),
    session: AsyncSession = _session_dep,
    read_ctx: ConventionsReadContext = _read_ctx_dep,
) -> HTMLResponse:
    """``GET /ui/conventions`` -- list page or HTMX table fragment."""
    return await render_index(request, read_ctx, session=session, kind=kind)


async def _detail_handler(
    request: Request,
    slug: str,
    session: AsyncSession = _session_dep,
    read_ctx: ConventionsReadContext = _read_ctx_dep,
) -> HTMLResponse:
    """``GET /ui/conventions/{slug}`` -- detail page or HTMX body fragment."""
    validate_slug(slug)
    return await render_detail(request, read_ctx, session=session, slug=slug)


# ---------------------------------------------------------------------------
# T2 (#1896) -- write surface: author / edit / preview / delete + history
# ---------------------------------------------------------------------------


async def _create_modal_handler(
    request: Request,
    write_ctx: ConventionsWriteContext = _write_ctx_dep,
) -> HTMLResponse:
    """``GET /ui/conventions/create`` -- HTMX author modal fragment."""
    return await render_create_modal(request, write_ctx)


async def _create_submit_handler(
    request: Request,
    slug: str = Form(..., max_length=SLUG_MAX_LENGTH),
    title: str = Form(..., max_length=TITLE_MAX_LENGTH),
    body: str = Form(..., max_length=BODY_MAX_LENGTH),
    kind: str = Form(..., max_length=32),
    priority: int = Form(default=0, ge=PRIORITY_MIN, le=PRIORITY_MAX),
    session: AsyncSession = _raw_session_dep,
    write_ctx: ConventionsWriteContext = _write_ctx_dep,
) -> HTMLResponse:
    """``POST /ui/conventions/create`` -- author a convention via the service."""
    return await submit_create(
        request,
        write_ctx,
        session=session,
        slug=slug,
        title=title,
        body=body,
        kind=kind,
        priority=priority,
    )


async def _preview_handler(
    request: Request,
    body: str = Form(default="", max_length=BODY_MAX_LENGTH),
    kind: str = Form(default="operational", max_length=32),
    write_ctx: ConventionsWriteContext = _write_ctx_dep,
) -> HTMLResponse:
    """``POST /ui/conventions/preview`` -- debounced token-cost preview.

    Gated on tenant_admin (the preview is part of the author / edit
    write flow); the chassis CSRF middleware already rejected an
    anonymous / cross-site POST.
    """
    del write_ctx  # gate only -- the preview render needs no operator state.
    return await render_token_preview(request, body=body, kind=kind)


async def _edit_modal_handler(
    request: Request,
    slug: str,
    session: AsyncSession = _raw_session_dep,
    write_ctx: ConventionsWriteContext = _write_ctx_dep,
) -> HTMLResponse:
    """``GET /ui/conventions/{slug}/edit`` -- HTMX edit modal fragment."""
    validate_slug(slug)
    return await render_edit_modal(request, write_ctx, session=session, slug=slug)


async def _delete_confirm_handler(
    request: Request,
    slug: str,
    session: AsyncSession = _raw_session_dep,
    write_ctx: ConventionsWriteContext = _write_ctx_dep,
) -> HTMLResponse:
    """``GET /ui/conventions/{slug}/delete`` -- HTMX delete-confirm gate."""
    validate_slug(slug)
    return await render_delete_confirm(request, write_ctx, session=session, slug=slug)


async def _history_handler(
    request: Request,
    slug: str,
    session: AsyncSession = _session_dep,
    read_ctx: ConventionsReadContext = _read_ctx_dep,
) -> HTMLResponse:
    """``GET /ui/conventions/{slug}/history`` -- HTMX history diff panel.

    Read surface (OPERATOR-tier) -- history is a read of the
    ``tenant_convention_history`` rows, gated like the detail view.
    """
    validate_slug(slug)
    return await render_history_panel(request, read_ctx, session=session, slug=slug)


async def _patch_handler(
    request: Request,
    slug: str,
    title: str = Form(..., max_length=TITLE_MAX_LENGTH),
    body: str = Form(..., max_length=BODY_MAX_LENGTH),
    priority: int = Form(..., ge=PRIORITY_MIN, le=PRIORITY_MAX),
    session: AsyncSession = _raw_session_dep,
    write_ctx: ConventionsWriteContext = _write_ctx_dep,
) -> HTMLResponse:
    """``PATCH /ui/conventions/{slug}`` -- apply an edit via the service.

    ``kind`` + ``slug`` are not in the form -- the PATCH surface cannot
    change them (the edit modal renders them read-only).
    """
    validate_slug(slug)
    return await submit_update(
        request,
        write_ctx,
        session=session,
        slug=slug,
        title=title,
        body=body,
        priority=priority,
    )


async def _delete_handler(
    request: Request,
    slug: str,
    session: AsyncSession = _raw_session_dep,
    write_ctx: ConventionsWriteContext = _write_ctx_dep,
) -> HTMLResponse:
    """``DELETE /ui/conventions/{slug}`` -- delete behind the confirm gate."""
    validate_slug(slug)
    return await delete_convention(
        request,
        write_ctx.operator,
        session=session,
        slug=slug,
    )


def _register_static_prefix_routes(router: APIRouter) -> None:
    """Wire the static-prefix routes onto *router*.

    Registration order is **load-bearing**: these static-prefix routes
    MUST register before :func:`_register_parametrized_routes`. The
    literal ``/ui/conventions`` list path and T2's
    ``/ui/conventions/create`` + ``/ui/conventions/preview`` live here so
    the literal segments are never bound to the ``{slug}`` parameter of
    the detail / edit / history routes (FastAPI matches the first route
    whose template fits, and ``{slug}`` would otherwise swallow the
    literal ``"create"`` / ``"preview"`` token).
    """
    router.add_api_route(
        "/ui/conventions",
        _list_handler,
        methods=["GET"],
        name="ui_conventions_list",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/conventions/create",
        _create_modal_handler,
        methods=["GET"],
        name="ui_conventions_create_modal",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/conventions/create",
        _create_submit_handler,
        methods=["POST"],
        name="ui_conventions_create_submit",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/conventions/preview",
        _preview_handler,
        methods=["POST"],
        name="ui_conventions_preview",
        response_class=HTMLResponse,
    )


def _register_parametrized_routes(router: APIRouter) -> None:
    """Wire the parametrised ``{slug}`` routes onto *router*.

    Must register **after** :func:`_register_static_prefix_routes` --
    the literal ``/ui/conventions`` + ``/create`` + ``/preview`` carry
    segments that would otherwise bind to the ``{slug}`` parameter and
    resolve the wrong handler. The ``/{slug}/edit``, ``/{slug}/delete``,
    and ``/{slug}/history`` sub-paths carry a trailing literal so they
    register cleanly after the bare detail route without a further
    ordering hazard.
    """
    router.add_api_route(
        "/ui/conventions/{slug}",
        _detail_handler,
        methods=["GET"],
        name="ui_conventions_detail",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/conventions/{slug}/edit",
        _edit_modal_handler,
        methods=["GET"],
        name="ui_conventions_edit_modal",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/conventions/{slug}/delete",
        _delete_confirm_handler,
        methods=["GET"],
        name="ui_conventions_delete_confirm",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/conventions/{slug}/history",
        _history_handler,
        methods=["GET"],
        name="ui_conventions_history",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/conventions/{slug}",
        _patch_handler,
        methods=["PATCH"],
        name="ui_conventions_patch",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/conventions/{slug}",
        _delete_handler,
        methods=["DELETE"],
        name="ui_conventions_delete",
        response_class=HTMLResponse,
    )


def build_conventions_router() -> APIRouter:
    """Construct the conventions UI :class:`APIRouter`.

    Factory function (not a module-level constant) so a test app can
    construct parallel routers without sharing route state -- mirrors
    the memory / broadcast / topology convention.

    Registration order is load-bearing for the static-prefix routes:
    they register before the parametrised routes so a literal segment in
    the URL doesn't bind to ``{slug}`` (see the module docstring).
    """
    router = APIRouter(tags=["ui-conventions"])
    _register_static_prefix_routes(router)
    _register_parametrized_routes(router)
    return router
