# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""FastAPI route handlers for the operator console.

Initiative #337 (G10.0 Frontend chassis), Task #866 (T5). The chassis
ships the umbrella :func:`build_router` that aggregates:

* :mod:`~meho_backplane.ui.routes.dashboard` -- ``GET /ui/`` --
  authenticated landing page with the 3x2 surface card grid, the
  HTMX SSE last-5-events snippet, and the version + readiness card.
* :mod:`~meho_backplane.ui.routes.memory` -- the memory surface
  (G10.4-T1 #877): ``/ui/memory`` list, ``/ui/memory/<scope>/<slug>``
  detail + edit-in-place + delete, ``/ui/memory/tags`` autocomplete.
* :mod:`~meho_backplane.ui.routes.stubs` -- ``GET /ui/{knowledge,
  connectors}`` -- remaining placeholder routes the surface Initiatives
  G10.2 (kb) + G10.3 (connectors) replace.

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
from meho_backplane.ui.routes.memory import build_memory_router
from meho_backplane.ui.routes.stubs import build_stubs_router
from meho_backplane.ui.routes.topology import build_router as build_topology_router

__all__ = [
    "build_broadcast_router",
    "build_dashboard_router",
    "build_memory_router",
    "build_router",
    "build_stubs_router",
    "build_topology_router",
]


def build_router() -> APIRouter:
    """Aggregate the dashboard + broadcast + topology + memory + stubs routers.

    Order matters: FastAPI matches by registration order, so a
    surface Initiative's real router is included **before** the
    stubs aggregate to win the first-match-wins path lookup. The
    dashboard ``/ui/`` route does not collide with any surface
    sub-path; broadcast lands ``/ui/broadcast`` + ``/ui/broadcast/stream``,
    topology lands ``/ui/topology`` + ``/ui/topology/node/{id}``, and
    memory lands ``/ui/memory`` + ``/ui/memory/{scope}/{slug}`` --
    each of which would otherwise hit a ``/ui/{slug}`` stub. The
    ``broadcast`` / ``memory`` stubs are dropped from the stubs
    enumeration so the real routes are the only registrations in the
    OpenAPI schema. Once G10.2-G10.3 land their surface routers, each
    includes itself ahead of the stubs the same way.
    """
    router = APIRouter()
    router.include_router(build_dashboard_router())
    # Surface routers ahead of stubs -- their concrete paths win
    # the match against the stubs' placeholder ``/ui/{slug}``.
    router.include_router(build_broadcast_router())
    router.include_router(build_topology_router())
    router.include_router(build_memory_router())
    router.include_router(build_stubs_router())
    return router
