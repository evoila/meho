# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Connectors UI routes: targets list + per-target detail + re-probe.

Initiative #340 (G10.3 Connectors + Targets UI). Task #873 (T1) ships
the **read** surface: sortable / filterable targets table, the
per-target detail page (full row + fingerprint card + recent-ops
SSE-live + available-operations matrix), and a tenant_admin-gated
re-probe action. T2 (#874) layers create / edit forms, T3 (#875)
layers bulk import.

Module layout:

* :mod:`~meho_backplane.ui.routes.connectors.list_view` -- the
  ``GET /ui/connectors`` route. One handler serves both shapes:
  the full page (browser nav) and the table-rows fragment (HTMX
  sort / filter swap, branch on ``HX-Request``).
* :mod:`~meho_backplane.ui.routes.connectors.detail` -- the
  ``GET /ui/connectors/{name}`` route. Renders the detail page: the
  full target row, the fingerprint card, the recent-ops card
  (last 10 audit rows, SSE-live filtered to ``target=<name>``),
  and the available-operations matrix grouped by ``operation_group``.
* :mod:`~meho_backplane.ui.routes.connectors.probe` -- the
  ``POST /ui/connectors/{name}/probe`` route. Tenant_admin-gated
  re-probe verb that re-runs the connector ``fingerprint`` step,
  persists the result to ``targets.fingerprint``, and returns the
  refreshed ``_fingerprint_card.html`` fragment for an HTMX swap.
  Uses the same :func:`~meho_backplane.connectors.resolver
  .resolve_connector_or_label` helper the ``/api/v1/targets/{name}/probe``
  REST route does so the two surfaces stay byte-compatible.
* :mod:`~meho_backplane.ui.routes.connectors.forms_router` -- the
  T2 (#874) create / edit form routes
  (``GET``/``POST`` ``/ui/connectors/create`` +
  ``GET /ui/connectors/{name}/edit`` + ``PATCH /ui/connectors/{name}``).
  Tenant_admin-gated server-side; the DaisyUI modal forms HTMX-submit
  into the REST ``POST``/``PATCH`` ``/api/v1/targets`` handlers
  in-process so the UI and REST surfaces share one validation +
  product-check + audit code path.
* :mod:`~meho_backplane.ui.routes.connectors.import_router` -- the
  T3 (#875) bulk-import routes (``GET``/``POST`` ``/ui/connectors/import``
  + ``POST /ui/connectors/import/confirm``). Tenant_admin-gated
  server-side; the operator pastes / uploads a ``targets.yaml`` which
  is parsed (``yaml.safe_load``), classified CREATE-vs-UPDATE, and on
  confirm applied **in-process** via ``create_target`` / ``update_target``
  -- mirroring the client-orchestrated CRUD the ``meho targets import``
  CLI (#257) performs (there is no ``/api/v1/targets/import`` endpoint).

The umbrella :func:`build_router` aggregates all five. It is mounted
**before** :func:`~meho_backplane.ui.routes.stubs.build_stubs_router`
in :func:`~meho_backplane.ui.routes.build_router` so the real
``/ui/connectors`` and ``/ui/connectors/{name}`` handlers win the
first-match-wins path lookup. The ``connectors`` stub is retired by
this task.

Tenant scoping is non-overrideable. Every target query passes
``session_ctx.tenant_id`` from the chassis-validated
:class:`UISessionContext`; no query parameter or path segment carries
a tenant id. The recent-ops SSE feed flows through the existing
``/ui/broadcast/stream`` bridge (G10.1) which is itself session-gated
on the same boundary -- a target name typed into the URL bar that
belongs to another tenant returns 404 from the detail handler before
the SSE wiring even renders.
"""

from __future__ import annotations

from fastapi import APIRouter

from meho_backplane.ui.routes.connectors.detail import build_detail_router
from meho_backplane.ui.routes.connectors.forms_router import build_forms_router
from meho_backplane.ui.routes.connectors.import_router import build_import_router
from meho_backplane.ui.routes.connectors.list_view import build_list_router
from meho_backplane.ui.routes.connectors.probe import build_probe_router
from meho_backplane.ui.routes.connectors.registry_actions import build_registry_actions_router
from meho_backplane.ui.routes.connectors.registry_list import build_registry_list_router

__all__ = ["build_router"]


def build_router() -> APIRouter:
    """Aggregate the connectors UI routes into one ``/ui/connectors*`` router.

    Factory function (not a module-level constant) so a test app can
    construct multiple parallel routers without sharing route state --
    mirrors the chassis convention in
    :mod:`meho_backplane.ui.routes.topology` /
    :mod:`meho_backplane.ui.routes.memory`.

    Registration order is **load-bearing**: the literal ``GET /ui/connectors``
    list route registers before the parametrised ``GET /ui/connectors/{name}``
    detail route so the empty-tail URL is matched as the list rather than
    captured with ``name=""``. The T2 forms router is included **before**
    the detail router for the same reason -- its literal
    ``GET /ui/connectors/create`` route must win the first-match-wins
    lookup over ``GET /ui/connectors/{name}`` (otherwise ``"create"``
    binds to ``name``). The T3 import router is included for the same
    reason -- its literal ``/ui/connectors/import`` /
    ``/ui/connectors/import/confirm`` routes must win the first-match
    lookup over ``GET /ui/connectors/{name}`` (otherwise ``"import"``
    binds to ``name``). The G10.13-T1 (#1885) registry routers are
    included for the same reason -- the literal ``/ui/connectors/registry``
    list route and the literal-suffixed per-row action routes
    (``/ui/connectors/registry/{connector_id}/enable`` etc.) must win the
    first-match-wins lookup over ``GET /ui/connectors/{name}`` (otherwise
    ``"registry"`` binds to ``name`` and the registry list 404s through
    the detail handler). The probe route's path is fully literal
    (``/ui/connectors/{name}/probe``) so the ``POST`` verb plus the
    extra ``/probe`` segment makes it unambiguous regardless of order;
    we still include it last for the same readability convention the
    memory router uses. The forms router's ``PATCH /ui/connectors/{name}``
    shares the detail route's path but is distinguished by HTTP method,
    so its ordering relative to the detail ``GET`` is not load-bearing.
    """
    router = APIRouter()
    router.include_router(build_list_router())
    router.include_router(build_forms_router())
    router.include_router(build_import_router())
    router.include_router(build_registry_list_router())
    router.include_router(build_registry_actions_router())
    router.include_router(build_detail_router())
    router.include_router(build_probe_router())
    return router
