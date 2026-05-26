# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""FastAPI route handlers for the operator console.

Initiative #337 (G10.0 Frontend chassis), Task #866 (T5). The chassis
ships the umbrella :func:`build_router` that aggregates:

* :mod:`~meho_backplane.ui.routes.dashboard` -- ``GET /ui/`` --
  authenticated landing page with the 3x2 surface card grid, the
  HTMX SSE last-5-events snippet, and the version + readiness card.
* :mod:`~meho_backplane.ui.routes.kb` -- ``GET /ui/kb``,
  ``POST /ui/kb/search``, ``GET /ui/kb/<slug>``,
  ``GET /ui/kb/<slug>/preview`` -- KB read surface (G10.2-T1 #870).
* :mod:`~meho_backplane.ui.routes.stubs` -- ``GET /ui/{connectors,
  memory}`` -- remaining placeholder routes the surface Initiatives
  G10.3-G10.4 replace. ``broadcast`` / ``topology`` / ``knowledge``
  stubs retired once their real routers land.

Auth surfaces (``/ui/auth/login``, ``/ui/auth/callback``,
``/ui/auth/logout``) live under
:mod:`meho_backplane.ui.auth.routes` and are aggregated separately;
T5's :func:`meho_backplane.main` ``include_router`` block mounts both
routers.

The router factory pattern (rather than module-level constants)
mirrors :func:`meho_backplane.ui.auth.routes.build_router` so a
test app can construct multiple parallel routers without sharing
route state -- handy for the chassis smoke test's "minimal app"
fixture that wires UI middleware + UI router without dragging the
full backplane app in.
"""

from __future__ import annotations

from fastapi import APIRouter

from meho_backplane.ui.routes.broadcast import build_router as build_broadcast_router
from meho_backplane.ui.routes.dashboard import build_dashboard_router
from meho_backplane.ui.routes.kb import build_kb_router
from meho_backplane.ui.routes.stubs import build_stubs_router
from meho_backplane.ui.routes.topology import build_router as build_topology_router

__all__ = [
    "build_broadcast_router",
    "build_dashboard_router",
    "build_kb_router",
    "build_router",
    "build_stubs_router",
    "build_topology_router",
]


def build_router() -> APIRouter:
    """Aggregate the dashboard + surface + stubs routers.

    Order matters: FastAPI matches by registration order, so a
    surface Initiative's real router is included **before** the
    stubs aggregate to win the first-match-wins path lookup. The
    dashboard ``/ui/`` route does not collide with any surface
    sub-path; broadcast lands ``/ui/broadcast`` + ``/ui/broadcast/stream``
    and topology lands ``/ui/topology`` + ``/ui/topology/node/{id}``,
    each of which would otherwise hit a ``/ui/{slug}`` stub. The
    ``broadcast`` / ``topology`` / ``knowledge`` stubs are retired
    once their real routers land (broadcast: #867, topology: #880,
    kb: #870). Remaining stubs cover ``connectors`` and ``memory``.
    """
    router = APIRouter()
    router.include_router(build_dashboard_router())
    # Surface routers ahead of stubs -- their concrete paths win
    # the match against the stubs' placeholder ``/ui/{slug}``.
    router.include_router(build_broadcast_router())
    router.include_router(build_topology_router())
    router.include_router(build_kb_router())
    router.include_router(build_stubs_router())
    return router
