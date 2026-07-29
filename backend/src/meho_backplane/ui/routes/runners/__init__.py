# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Runners UI route: the satellite-runner fleet page (Initiative #2415, Task #2589).

The operator-console face of the push-only satellite-runner gateway (#2415).
The gateway shipped the runner mode (#2497), scoped principals (#2502), the
command plane (#2498), and the dead-man switch (#2501) with no console
visibility -- the only fleet views were ``meho runner-principal list`` and the
Bearer ``GET /api/v1/runner-principals`` route, neither of which surfaced
liveness. This adds the read-only ``/ui/runners`` page on the established
HTMX/Jinja chassis, answering "which runners are alive?" at a glance.

Why a session BFF and not the Bearer ``/api/v1/runner-principals`` route
----------------------------------------------------------------------

A browser carrying only the BFF session cookie cannot authenticate the
Bearer-gated REST route. So this module's sub-route is
``require_ui_session``-gated and calls the in-process
:class:`~meho_backplane.auth.runner_principals.RunnerPrincipalService`
``list_`` (the same accessor the REST surface uses), plus a
``runner_assignments.stale_at`` dead-man lookup
(:func:`~meho_backplane.gateway.repository.get_stale_markers`) joined on the
runner name -- the same console-surface pattern the checks / scheduler
surfaces use.

Module layout
-------------

* :mod:`~meho_backplane.ui.routes.runners.list_view` -- ``GET /ui/runners``.
  One handler serves the full page (browser nav) and the table-rows fragment
  (30s auto-refresh poll). Operator-readable.
* :mod:`~meho_backplane.ui.routes.runners.views` -- row-to-view projection +
  liveness derivation (revoked / dead-man / live), reusing the checks
  surface's badge vocabulary + UTC coercion.

Out of scope (v1): no register / revoke affordances -- ``meho
runner-principal`` (#2502) is the single write path; no assignment/workload
detail, capability listing, or runner metrics (#2589 out-of-scope list).
"""

from __future__ import annotations

from fastapi import APIRouter

from meho_backplane.ui.routes.runners.list_view import build_list_router

__all__ = ["build_runners_router"]


def build_runners_router() -> APIRouter:
    """Aggregate the runners UI routes into one ``/ui/runners`` router.

    Factory function (not a module-level constant) so a test app can construct
    parallel routers without sharing route state -- the chassis convention
    every surface router follows.
    """
    router = APIRouter()
    router.include_router(build_list_router())
    return router
