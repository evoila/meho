# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Memory UI route registration -- maps HTTP verbs to the render helpers.

Initiative #341 (G10.4 Memory UI), Task #877 (T1). The route handlers
in this module are thin wrappers: parse FastAPI params, resolve the
session / operator dependency, and hand off to the render functions
in :mod:`~meho_backplane.ui.routes.memory.views`. Splitting the
registration here from the render logic in ``views`` keeps each module
under the chassis-wide ~600-line + ~100-line caps and gives the
render helpers a unit-testable seam (no FastAPI :class:`Request`
fixture required for projection logic).

Route inventory
---------------

* ``GET /ui/memory`` -- list page or HTMX card-list fragment.
* ``GET /ui/memory/tags`` -- HTMX datalist for the tag autocomplete.
* ``GET /ui/memory/<scope>/<slug>`` -- detail page or HTMX body fragment.
* ``GET /ui/memory/<scope>/<slug>/edit`` -- HTMX edit-form fragment.
* ``PATCH /ui/memory/<scope>/<slug>`` -- HTMX save the edited body.
* ``DELETE /ui/memory/<scope>/<slug>`` -- HTMX delete + re-render the list.

Tenant + RBAC + info-leak posture are documented on the render
functions themselves; this module owns only the path / method /
dependency wiring.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse

from meho_backplane.auth.operator import Operator
from meho_backplane.memory import MemoryScope
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.routes.memory.operator import resolve_ui_operator
from meho_backplane.ui.routes.memory.views import (
    BODY_MAX_LENGTH,
    SCOPE_ALL,
    SLUG_MAX_LENGTH,
    delete_entry,
    patch_entry,
    render_detail,
    render_edit_form,
    render_index,
    render_tags,
    validate_slug,
)

__all__ = ["build_memory_router"]

#: Module-level :class:`Depends` closures -- ruff B008 idiom mirroring
#: the chassis dashboard + topology routes (no calls in default
#: argument positions).
_require_session_dep = Depends(require_ui_session)
_resolve_operator_dep = Depends(resolve_ui_operator)


async def _list_handler(
    request: Request,
    scope: str = Query(default=SCOPE_ALL, max_length=64),
    tag: str | None = Query(default=None, max_length=SLUG_MAX_LENGTH),
    session_ctx: UISessionContext = _require_session_dep,
) -> HTMLResponse:
    """``GET /ui/memory`` -- list page or HTMX card-list fragment."""
    return await render_index(request, session_ctx, scope=scope, tag=tag)


async def _tags_handler(
    request: Request,
    session_ctx: UISessionContext = _require_session_dep,
) -> HTMLResponse:
    """``GET /ui/memory/tags`` -- HTMX datalist autocomplete fragment."""
    return await render_tags(request, session_ctx)


async def _detail_handler(
    request: Request,
    scope: MemoryScope,
    slug: str,
    session_ctx: UISessionContext = _require_session_dep,
) -> HTMLResponse:
    """``GET /ui/memory/<scope>/<slug>`` -- detail page or HTMX body fragment."""
    validate_slug(slug)
    return await render_detail(request, session_ctx, scope=scope, slug=slug)


async def _edit_form_handler(
    request: Request,
    scope: MemoryScope,
    slug: str,
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _resolve_operator_dep,
) -> HTMLResponse:
    """``GET /ui/memory/<scope>/<slug>/edit`` -- HTMX edit-form fragment."""
    validate_slug(slug)
    return await render_edit_form(request, session_ctx, operator, scope=scope, slug=slug)


async def _patch_handler(
    request: Request,
    scope: MemoryScope,
    slug: str,
    body: str = Form(..., max_length=BODY_MAX_LENGTH),
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _resolve_operator_dep,
) -> HTMLResponse:
    """``PATCH /ui/memory/<scope>/<slug>`` -- save the edited body."""
    validate_slug(slug)
    return await patch_entry(request, session_ctx, operator, scope=scope, slug=slug, new_body=body)


async def _delete_handler(
    request: Request,
    scope: MemoryScope,
    slug: str,
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _resolve_operator_dep,
) -> HTMLResponse:
    """``DELETE /ui/memory/<scope>/<slug>`` -- delete + re-render the list."""
    validate_slug(slug)
    return await delete_entry(request, session_ctx, operator, scope=scope, slug=slug)


def _register_read_routes(router: APIRouter) -> None:
    """Wire the GET routes (list / tags / detail / edit-form) onto *router*.

    Split out of :func:`build_memory_router` so the factory body stays
    under the chassis-wide ~100-line cap. ``ui_memory_*`` names match
    the per-handler closures so a future ``url_for(...)`` from a
    template resolves cleanly.
    """
    router.add_api_route(
        "/ui/memory",
        _list_handler,
        methods=["GET"],
        name="ui_memory_list",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/memory/tags",
        _tags_handler,
        methods=["GET"],
        name="ui_memory_tags",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/memory/{scope}/{slug}",
        _detail_handler,
        methods=["GET"],
        name="ui_memory_detail",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/memory/{scope}/{slug}/edit",
        _edit_form_handler,
        methods=["GET"],
        name="ui_memory_edit_form",
        response_class=HTMLResponse,
    )


def _register_write_routes(router: APIRouter) -> None:
    """Wire the PATCH + DELETE routes onto *router*.

    Split out of :func:`build_memory_router` for the same reason as
    :func:`_register_read_routes`. The PATCH route shares the
    ``/ui/memory/{scope}/{slug}`` path with the detail GET; FastAPI
    distinguishes by method so registration order is not load-bearing.
    """
    router.add_api_route(
        "/ui/memory/{scope}/{slug}",
        _patch_handler,
        methods=["PATCH"],
        name="ui_memory_patch",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/memory/{scope}/{slug}",
        _delete_handler,
        methods=["DELETE"],
        name="ui_memory_delete",
        response_class=HTMLResponse,
    )


def build_memory_router() -> APIRouter:
    """Construct the memory UI :class:`APIRouter`.

    Factory function (not a module-level constant) so a test app can
    construct parallel routers without sharing route state -- mirrors
    the broadcast / topology / dashboard convention.
    """
    router = APIRouter(tags=["ui-memory"])
    _register_read_routes(router)
    _register_write_routes(router)
    return router
