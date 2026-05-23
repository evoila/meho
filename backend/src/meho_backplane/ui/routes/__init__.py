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

__all__ = ["build_dashboard_router", "build_router", "build_stubs_router"]


def build_router() -> APIRouter:
    """Aggregate the dashboard + stubs routers into a single ``/ui/*`` router.

    Order matters for diagnostic clarity (FastAPI matches by
    registration order on conflict; the dashboard ``/ui/`` does not
    collide with the stub ``/ui/{slug}`` routes, but a future surface
    Initiative replacing a stub will register *before* this aggregate
    to win the match).
    """
    router = APIRouter()
    router.include_router(build_dashboard_router())
    router.include_router(build_stubs_router())
    return router
