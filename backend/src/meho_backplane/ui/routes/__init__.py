# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""FastAPI route handlers for the operator console.

Initiative #337 (G10.0 Frontend chassis), Task #866 (T5). The chassis
ships the umbrella :func:`build_router` that aggregates:

* :mod:`~meho_backplane.ui.routes.dashboard` -- ``GET /ui/`` --
  authenticated landing page with the 3x2 surface card grid, the
  HTMX SSE last-5-events snippet, and the version + readiness card.
* :mod:`~meho_backplane.ui.routes.stubs` -- ``GET /ui/{broadcast,
  knowledge,topology,connectors,memory}`` -- placeholder routes the
  surface Initiatives G10.1-G10.5 replace.

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

from meho_backplane.ui.routes.dashboard import build_dashboard_router
from meho_backplane.ui.routes.stubs import build_stubs_router
from meho_backplane.ui.routes.topology import build_router as build_topology_router

__all__ = [
    "build_dashboard_router",
    "build_router",
    "build_stubs_router",
    "build_topology_router",
]


def build_router() -> APIRouter:
    """Aggregate the dashboard + topology + stubs routers under ``/ui/*``.

    Order matters: FastAPI matches by registration order, so a
    surface Initiative's real router is included **before** the
    stubs aggregate to win the first-match-wins path lookup. The
    dashboard ``/ui/`` route does not collide with any surface
    sub-path; topology lands ``/ui/topology`` and
    ``/ui/topology/node/{id}`` which would otherwise hit the
    ``/ui/topology`` stub. Once G10.1-G10.4 land their surface
    routers, each includes itself ahead of the stubs the same way.
    """
    router = APIRouter()
    router.include_router(build_dashboard_router())
    # Surface routers ahead of stubs -- their concrete paths win
    # the match against the stubs' placeholder ``/ui/{slug}``.
    router.include_router(build_topology_router())
    router.include_router(build_stubs_router())
    return router
