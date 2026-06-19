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

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_session
from meho_backplane.ui.routes.conventions.operator import (
    ConventionsReadContext,
    resolve_read_context,
)
from meho_backplane.ui.routes.conventions.views import (
    KIND_ALL,
    render_detail,
    render_index,
    validate_slug,
)

__all__ = ["build_conventions_router"]

#: Module-level :class:`Depends` closures -- ruff B008 idiom mirroring
#: the memory + topology routes (no calls in default argument positions).
_session_dep = Depends(get_session)
_read_ctx_dep = Depends(resolve_read_context)


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


def _register_static_prefix_routes(router: APIRouter) -> None:
    """Wire the static-prefix routes onto *router*.

    Registration order is **load-bearing**: these static-prefix routes
    MUST register before :func:`_register_parametrized_routes`. The
    literal ``/ui/conventions`` list path lives here; T2 (#1838-T2) adds
    ``/ui/conventions/create`` + ``/ui/conventions/preview`` to this
    group so the literal segments are never bound to the ``{slug}``
    parameter of the detail route.
    """
    router.add_api_route(
        "/ui/conventions",
        _list_handler,
        methods=["GET"],
        name="ui_conventions_list",
        response_class=HTMLResponse,
    )


def _register_parametrized_routes(router: APIRouter) -> None:
    """Wire the parametrised ``{slug}`` routes onto *router*.

    Must register **after** :func:`_register_static_prefix_routes` --
    the literal ``/ui/conventions`` (and T2's ``/create`` + ``/preview``)
    carry segments that would otherwise bind to the ``{slug}`` parameter
    and resolve the wrong handler.
    """
    router.add_api_route(
        "/ui/conventions/{slug}",
        _detail_handler,
        methods=["GET"],
        name="ui_conventions_detail",
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
