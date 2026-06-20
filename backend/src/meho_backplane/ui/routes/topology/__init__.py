# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Topology UI routes: tabular view + Cytoscape graph + node detail drawer.

Initiative #342 (G10.5 Topology UI). Task #880 (G10.5-T1) shipped the
tabular surface + drawer; Task #881 (G10.5-T2) layered the Cytoscape.js
graph view on the same ``/ui/topology`` path via the ``?view=graph``
branch. T3 (#882) adds dependents / path query overlays on top of the
graph.

Module layout:

* :mod:`~meho_backplane.ui.routes.topology.table` -- the
  ``GET /ui/topology`` route. One handler serves three response
  shapes: the full tabular page (browser nav, ``view=table``), the
  ``_table_rows.html`` HTMX fragment (sort / filter swap), and the
  Cytoscape graph full page (``view=graph``). The graph branch
  delegates to ``graph.render_graph``.
* :mod:`~meho_backplane.ui.routes.topology.graph` -- the
  ``?view=graph`` render. Pulls nodes via the substrate
  :func:`list_nodes` (capped at 500 per Initiative #342 work item
  #6) and edges via a local SQLite-portable ORM query, then emits
  the Cytoscape elements as a ``<script type="application/json">``
  data island the ``topology-graph.js`` Alpine controller reads on
  init.
* :mod:`~meho_backplane.ui.routes.topology.detail` -- the
  ``GET /ui/topology/node/{node_id}`` route. Renders the side-drawer
  fragment shared by both the table view (HTMX row "View" button)
  and the graph view (HTMX node-tap handler) with node properties,
  incoming/outgoing edges, recent audit operations on the node's
  target (when one is attached), and a "show dependents" link.
* :mod:`~meho_backplane.ui.routes.topology.edges` -- the curated-edge
  **write** routes (``GET /ui/topology/edges/annotate`` modal,
  ``POST /ui/topology/edges`` annotate, ``DELETE
  /ui/topology/edges/{edge_id}`` unannotate). ``require_ui_session`` +
  CSRF-gated + ``tenant_admin`` (Initiative #1941 Task #1953); calls the
  :mod:`~meho_backplane.topology.annotate` service in-process. The literal
  ``edges`` / ``annotate`` segments are registered **before** the detail
  router's ``node/{node_id}`` param route so the first-match-wins lookup
  never binds them as a node id.

The umbrella :func:`build_router` aggregates all three. It is mounted
**before** :func:`meho_backplane.ui.routes.stubs.build_stubs_router`
in :func:`meho_backplane.ui.routes.build_router` so the real
``/ui/topology`` handler wins the first-match-wins lookup -- a later
``include_router`` for the stub does **not** override an earlier
registration (verified at construction time with a FastAPI test
client). The remaining three stub surfaces stay placeholder routes
until their own G10.x Initiatives land.
"""

from __future__ import annotations

from fastapi import APIRouter

from meho_backplane.ui.routes.topology.detail import build_detail_router
from meho_backplane.ui.routes.topology.edges import build_edges_router
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
    # Edge-write routes BEFORE the detail router: the literal
    # ``/ui/topology/edges/annotate`` must win the first-match-wins lookup
    # against ``detail.py``'s ``/ui/topology/node/{node_id}`` param route.
    # (The two literal segments ``edges`` / ``annotate`` cannot bind as a
    # ``{node_id}`` UUID anyway, but registering ahead keeps the ordering
    # discipline explicit and robust against a future bare ``{param}``
    # route landing in this aggregator.)
    router.include_router(build_edges_router())
    router.include_router(build_detail_router())
    return router
