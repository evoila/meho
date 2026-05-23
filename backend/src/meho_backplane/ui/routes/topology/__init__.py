# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Topology UI routes: tabular view + node detail drawer.

Initiative #342 (G10.5 Topology UI), Task #880 (G10.5-T1). This subpackage
ships the read-only tabular surface plus the per-node drawer the
chassis stub (#866) placeholders for. The Cytoscape.js graph view + the
table-graph cross-link land in T2 (#881); the dependents / path query
overlays land in T3 (#882).

Module layout:

* :mod:`~meho_backplane.ui.routes.topology.table` -- the
  ``GET /ui/topology`` route. Renders the full HTML page on a normal
  GET and an HTMX fragment of just the table body on an
  ``HX-Request`` round trip so sort / filter changes swap into the
  same DOM tree without a full re-render.
* :mod:`~meho_backplane.ui.routes.topology.detail` -- the
  ``GET /ui/topology/node/{node_id}`` route. Renders the side-drawer
  fragment with node properties, incoming/outgoing edges, recent
  audit operations on the node's target (when one is attached), and
  a "show dependents" link that hands off to the future T3 graph
  view.

The umbrella :func:`build_router` aggregates both. It is mounted
**before** :func:`meho_backplane.ui.routes.stubs.build_stubs_router`
in :func:`meho_backplane.ui.routes.build_router` so the real
``/ui/topology`` handler wins the first-match-wins lookup -- a later
``include_router`` for the stub does **not** override an earlier
registration (verified at construction time with a FastAPI test
client). The remaining four stub surfaces stay placeholder routes
until their own G10.x Initiatives land.
"""

from __future__ import annotations

from fastapi import APIRouter

from meho_backplane.ui.routes.topology.detail import build_detail_router
from meho_backplane.ui.routes.topology.table import build_table_router

__all__ = ["build_router"]


def build_router() -> APIRouter:
    """Aggregate the topology UI routes into a single ``/ui/topology*`` router.

    Factory function (not a module-level constant) so a test app can
    construct multiple parallel routers without sharing route state --
    mirrors the chassis convention in
    :mod:`meho_backplane.ui.routes.dashboard` and
    :mod:`meho_backplane.ui.routes.stubs`.
    """
    router = APIRouter()
    router.include_router(build_table_router())
    router.include_router(build_detail_router())
    return router
