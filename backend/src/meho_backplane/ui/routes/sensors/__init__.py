# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Sensors UI route: the Sensor registry page (Initiative #2416, Task #2591).

The operator-console face of the check layer's registration substrate (#2503).
The check layer shipped the Sensor registry, the latest-result projection on
the row, and the ``/ui/checks`` Dashboard rollup (#2506), but a Sensor surfaced
in the console only via a Dashboard that composed it -- a registered-but-
uncomposed Sensor was invisible. This adds the read-only ``/ui/sensors``
registry page on the established HTMX/Jinja chassis, answering "which sensors
exist / which are failing / which did I register but never compose?" at a
glance.

Why a session BFF and not the Bearer ``/api/v1/sensors`` route
--------------------------------------------------------------

A browser carrying only the BFF session cookie cannot authenticate the
Bearer-gated REST route. So this module's sub-route is
``require_ui_session``-gated and calls the in-process
:class:`~meho_backplane.checks.service.SensorAdminService` ``list_`` (the same
accessor the REST surface uses, carrying the same ``status`` / ``cadence_kind``
filters) -- the same console-surface pattern the checks / runners surfaces use.

Module layout
-------------

* :mod:`~meho_backplane.ui.routes.sensors.list_view` -- ``GET /ui/sensors``.
  One handler serves the full page (browser nav) and the table-rows fragment
  (filter swap + 30s auto-refresh poll). Operator-readable.
* :mod:`~meho_backplane.ui.routes.sensors.views` -- row-to-view projection +
  cadence rendering, reusing the checks surface's five-state badge vocabulary +
  UTC coercion.

Out of scope (v1): no per-Sensor detail page (there is no REST GET-by-id, by
design); no create / edit / delete affordances -- ``meho sensor`` (#2503) is
the single write path; "edit" is delete + recreate.
"""

from __future__ import annotations

from fastapi import APIRouter

from meho_backplane.ui.routes.sensors.list_view import build_list_router

__all__ = ["build_sensors_router"]


def build_sensors_router() -> APIRouter:
    """Aggregate the sensors UI routes into one ``/ui/sensors`` router.

    Factory function (not a module-level constant) so a test app can construct
    parallel routers without sharing route state -- the chassis convention
    every surface router follows.
    """
    router = APIRouter()
    router.include_router(build_list_router())
    return router
