# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Scheduler UI routes: trigger list + detail + create modal + cancel.

Initiative #1824 (G10.8 Autonomous execution control plane), Task #1826
(T6). This surface is the operator-console face of the G11 scheduler
(Goal #800): the scheduler runtime -- the trigger model, the cron / one-off
fire paths, the admin service -- already exists (`scheduler/service.py`,
`api/v1/scheduler.py`), but an operator could only define, inspect, and
cancel triggers from the CLI / REST. This task adds the `/ui/scheduler`
read + write surface on the established HTMX/Jinja chassis.

Why a session BFF and not the Bearer ``/api/v1/scheduler/*`` routes
-------------------------------------------------------------------

The REST scheduler routes are Bearer-gated over a verified JWT; a browser
carrying only the BFF session cookie + the CSRF double-submit token cannot
authenticate them. So this module adds ``/ui/scheduler`` sub-routes that
are ``require_ui_session`` + CSRF-gated and call the in-process
:class:`~meho_backplane.scheduler.service.SchedulerAdminService` (the same
``list_`` / ``get`` / ``create`` / ``cancel`` the REST + MCP + CLI surfaces
share) -- the same console-surface pattern the connectors / memory /
approvals surfaces use. The in-process call keeps the synchronous-audit
binding and avoids a self-HTTP hop the cookie could not auth anyway.

Module layout
-------------

* :mod:`~meho_backplane.ui.routes.scheduler.list_view` -- ``GET
  /ui/scheduler``. One handler serves the full page (browser nav) and the
  table-rows fragment (HTMX filter swap). Operator-readable.
* :mod:`~meho_backplane.ui.routes.scheduler.detail` -- ``GET
  /ui/scheduler/{id}``. The full trigger row + a tenant_admin-gated cancel
  affordance (hidden on a terminal trigger). Operator-readable.
* :mod:`~meho_backplane.ui.routes.scheduler.forms_router` -- the
  tenant_admin write routes: the create modal + live cron validate +
  create submit (:mod:`~meho_backplane.ui.routes.scheduler.create`) and
  the terminal cancel confirm + submit
  (:mod:`~meho_backplane.ui.routes.scheduler.cancel`).
* :mod:`~meho_backplane.ui.routes.scheduler.operator` -- re-exports the
  connectors surface's role gates (one JWT-lift implementation across the
  console).
* :mod:`~meho_backplane.ui.routes.scheduler.views` -- row-to-view
  projection + UTC coercion shared by the list + detail handlers.

Role tiers (per Initiative #1824)
---------------------------------

Reads (list / detail) = ``operator``. Writes (create / cancel) =
``tenant_admin``. The server-side 403 is the authority; the template
soft-hides the create button + cancel affordance from operators who can't
use them. **Cancel is terminal** -- a cancelled trigger never fires again
and there is no un-cancel path -- so it is fronted by a strong
native-``<dialog>`` confirm spelling out the irreversibility.

Out of scope (Initiative #1824): no pause / resume (the service has no
UPDATE path; only the dispatcher sets ``paused``), no edit (triggers are
immutable -- model "edit" as cancel + recreate), and no cross-tenant
``tenant_filter`` (platform_admin-only, needs the tenant selector T4 #865).
"""

from __future__ import annotations

from fastapi import APIRouter

from meho_backplane.ui.routes.scheduler.detail import build_detail_router
from meho_backplane.ui.routes.scheduler.forms_router import build_forms_router
from meho_backplane.ui.routes.scheduler.list_view import build_list_router

__all__ = ["build_scheduler_router"]


def build_scheduler_router() -> APIRouter:
    """Aggregate the scheduler UI routes into one ``/ui/scheduler*`` router.

    Factory function (not a module-level constant) so a test app can
    construct parallel routers without sharing route state -- the chassis
    convention every surface router follows.

    Registration order is **load-bearing**: the literal-prefix forms router
    (``/ui/scheduler/create``, ``/ui/scheduler/validate-cron``, and the
    ``/ui/scheduler/{id}/cancel`` pair) is included **before** the
    parametrised ``GET /ui/scheduler/{trigger_id}`` detail route so the
    literal ``"create"`` / ``"validate-cron"`` tokens are never captured as
    a trigger id. The list route's path is fully literal (``/ui/scheduler``)
    so its order is not load-bearing; it is registered first for the same
    readability convention the connectors router uses.
    """
    router = APIRouter()
    router.include_router(build_list_router())
    router.include_router(build_forms_router())
    router.include_router(build_detail_router())
    return router
