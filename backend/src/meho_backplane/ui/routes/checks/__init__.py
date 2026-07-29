# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Checks UI routes: Dashboard list + detail (Initiative #2416, Task #2506).

The operator-console face of the deterministic check layer (Goal #221's G10.x
console family): the Sensor registry (#2503), the rollup (#2506), and the REST
CRUD already exist, but an operator could only compose + inspect Dashboards
from the CLI / REST. This surface adds the ``/ui/checks`` read pages on the
established HTMX/Jinja chassis, answering "is everything OK?" at a glance.

Why a session BFF and not the Bearer ``/api/v1/checks/dashboards*`` routes
-------------------------------------------------------------------------

The REST routes are Bearer-gated over a verified JWT; a browser carrying only
the BFF session cookie cannot authenticate them. So this module's sub-routes
are ``require_ui_session``-gated and call the in-process
:class:`~meho_backplane.checks.dashboard_service.CheckDashboardAdminService`
(the same ``list_`` / ``get`` the REST surface uses) -- the same
console-surface pattern the scheduler / connectors surfaces use.

Module layout
-------------

* :mod:`~meho_backplane.ui.routes.checks.list_view` -- ``GET /ui/checks``.
  One handler serves the full page (browser nav) and the table-rows fragment
  (30s auto-refresh poll). Operator-readable.
* :mod:`~meho_backplane.ui.routes.checks.detail` -- ``GET
  /ui/checks/{dashboard_id}``. The rollup badge + member table.
  Operator-readable; cross-tenant / absent id collapses to 404.
* :mod:`~meho_backplane.ui.routes.checks.views` -- row-to-view projection +
  five-state badge vocabulary + UTC coercion shared by the list + detail
  handlers.

Out of scope (v1): no create / edit / delete affordances -- the REST CRUD is
the single write path; "edit" is delete + recreate (the Sensor / trigger
immutability posture). A console write modal is a G10.x follow-up if operators
ask.
"""

from __future__ import annotations

from fastapi import APIRouter

from meho_backplane.ui.routes.checks.detail import build_detail_router
from meho_backplane.ui.routes.checks.list_view import build_list_router

__all__ = ["build_checks_router"]


def build_checks_router() -> APIRouter:
    """Aggregate the checks UI routes into one ``/ui/checks*`` router.

    Factory function (not a module-level constant) so a test app can construct
    parallel routers without sharing route state -- the chassis convention
    every surface router follows. The literal ``GET /ui/checks`` list route is
    registered before the parametrised ``GET /ui/checks/{dashboard_id}``
    detail route so a first-match-wins lookup never shadows the list path.
    """
    router = APIRouter()
    router.include_router(build_list_router())
    router.include_router(build_detail_router())
    return router
