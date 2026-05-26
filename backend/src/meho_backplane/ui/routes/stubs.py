# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Surface-stub routes -- the five "coming soon" placeholders.

Initiative #337 (G10.0 Frontend chassis), Task #866 (T5). The chassis
ships routes at each of the five sidebar links so the navigation
shell renders end-to-end before the per-surface Initiatives
(G10.1-G10.5) replace them with the real views. Each stub returns
200 with a minimal HTML page that:

* Extends ``base.html`` so the chrome (navbar, sidebar, footer) is
  identical to every other surface.
* Displays a "Coming soon -- this surface ships with G10.x" panel
  pointing at the relevant Initiative number.
* Sets the same CSRF cookie the dashboard does, so a future
  state-changing form rendered before the surface Initiative lands
  has the double-submit chain in place from request one.

The remaining stub paths are kept consistent with the chassis
``base.html`` sidebar -- ``/ui/knowledge`` and ``/ui/connectors``.
``/ui/topology``, ``/ui/broadcast``, and ``/ui/memory`` are
intentionally NOT stubbed here: topology's real table view ships in
G10.5-T1 (#880), broadcast's real live-feed view ships in G10.1-T1
(#867), and memory's real list / detail / edit surface ships in
G10.4-T1 (#877), each owning its path; registering a stub for any
would shadow the real route in the generated OpenAPI schema. The Goal #336 done-when and
Initiative #337 work-item #5 reference these exact URLs.

Why one shared template
-----------------------

Each stub is a single template extension of ``base.html`` with the
surface metadata varied per route. A shared ``_stub.html`` partial
keeps the chassis from carrying five near-identical files; the
surface Initiatives replace the route (and may template-extend or
substitute entirely) without touching the others.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.templating import get_templates

__all__ = ["build_stubs_router"]

#: Type alias for the per-stub route handler closures. Each closure
#: accepts the FastAPI :class:`Request` + the injected session and
#: returns an :class:`HTMLResponse` -- ``Depends`` resolution happens
#: at request time so the signature has ``session`` as a kwarg.
_StubHandler = Callable[[Request, UISessionContext], Awaitable[HTMLResponse]]


@dataclass(frozen=True)
class _SurfaceStub:
    """Descriptor for one surface stub route.

    Frozen because the descriptors are module-level constants
    enumerated below; mutability would imply per-request mutation
    which the surface Initiatives explicitly do not need.
    """

    slug: str
    title: str
    initiative_number: int
    summary: str


_SURFACE_STUBS: Final[tuple[_SurfaceStub, ...]] = (
    _SurfaceStub(
        slug="knowledge",
        title="Knowledge",
        initiative_number=339,
        summary="Search + view + drag-and-drop upload + Markdown editor over the team kb.",
    ),
    _SurfaceStub(
        slug="connectors",
        title="Connectors",
        initiative_number=340,
        summary="Per-tenant target CRUD + per-target detail (fingerprint, last probe, ops).",
    ),
)


def _make_stub_handler(stub: _SurfaceStub) -> _StubHandler:
    """Build a closure rendering the placeholder page for *stub*.

    Each closure binds the surface's title / initiative / summary at
    construction time and pulls the operator's session at request
    time. The closure's return type is the typed
    :class:`HTMLResponse` so the route registration below can pin
    the OpenAPI response class without an explicit cast.
    """

    async def _render(
        request: Request,
        session: UISessionContext = Depends(require_ui_session),
    ) -> HTMLResponse:
        csrf_token = mint_csrf_token(str(session.session_id))
        context = {
            "page_title": stub.title,
            "surface_title": stub.title,
            "initiative_number": stub.initiative_number,
            "summary": stub.summary,
            "operator_sub": session.operator_sub,
            "csrf_token": csrf_token,
            # ``base.html``'s footer reads ``ready`` to colour the
            # readiness pill. Stubs do not poll readiness (the
            # dashboard owns that surface) -- ship ``False`` so the
            # ``StrictUndefined`` env does not raise on the read.
            "ready": False,
        }
        response = get_templates().TemplateResponse(request, "_stub.html", context)
        response.set_cookie(
            key=CSRF_COOKIE_NAME,
            value=csrf_token,
            httponly=False,
            secure=True,
            samesite="strict",
            path="/ui",
        )
        return response

    _render.__name__ = f"ui_stub_{stub.slug}"
    return _render


def build_stubs_router() -> APIRouter:
    """Construct the surface-stubs :class:`APIRouter`.

    Registers one ``GET`` route per :data:`_SURFACE_STUBS` entry; the
    surface Initiatives mount their own routers on top of this one
    (FastAPI's ``include_router`` ordering means a later router with
    the same path wins).
    """
    router = APIRouter(tags=["ui-stubs"])
    for stub in _SURFACE_STUBS:
        router.add_api_route(
            f"/ui/{stub.slug}",
            _make_stub_handler(stub),
            methods=["GET"],
            name=f"ui_stub_{stub.slug}",
            response_class=HTMLResponse,
        )
    return router
