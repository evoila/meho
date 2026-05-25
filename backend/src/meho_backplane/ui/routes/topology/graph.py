# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /ui/topology?view=graph`` -- the tenant topology Cytoscape graph view.

Initiative #342 (G10.5 Topology UI), Task #881 (G10.5-T2). Renders an
interactive node-link visualisation of the tenant's graph as a
Cytoscape.js island the operator can pan, zoom, and click. Reuses the
T1 (#880) :func:`meho_backplane.topology.query.list_nodes` substrate
plus a local SQLite-portable edge query (the substrate's
:func:`~meho_backplane.topology.query.list_edges` leans on PostgreSQL
``jsonb_typeof`` for the conflict predicate; the graph view does not
need conflict info so a leaner ORM query that runs on both dialects is
the right factoring -- same call as the T1 drawer's ``_fetch_edges``).

The route is the same handler as
:mod:`meho_backplane.ui.routes.topology.table` -- both modes share the
``GET /ui/topology`` path and branch on ``?view=`` so the URL contract
holds across the table/graph toggle and copy/paste reproduces either
mode. ``view`` is plumbed through :mod:`...topology.table` (the
shipped handler since T1) which delegates the graph branch here.

Layout strategy
---------------

The graph emits node + edge JSON into a ``<script type="application/json">``
data island; the Cytoscape init (``topology-graph.js``) reads it on
init and registers the two extension layouts (``cose-bilkent`` /
``dagre``) plus the built-in ``circle``. Server-side this module owns
only the data + the layout switcher chrome; layout selection is
client-state-only (an Alpine ``<select>`` calls
``cy.layout({name}).run()``).

500-node frontend cap
---------------------

Per Initiative #342 work item #6, the frontend renders at most
:data:`_GRAPH_NODE_CAP` (500) nodes. Beyond that the operator is
prompted to narrow the inventory or hand off to the T3 (#882) subgraph
query. The cap is enforced at the route by passing ``limit=500`` into
:func:`list_nodes`; the template surfaces a truncation banner when the
returned list is at the cap so an operator knows the rendered view is
incomplete.

Tenant scoping
--------------

Every read goes through :func:`list_nodes` (substrate-level
``WHERE graph_node.tenant_id = :tenant_id`` first clause, no override
path) plus the local :func:`_fetch_edges_for_nodes` (same
defence-in-depth as the T1 detail route: edge ``tenant_id`` + both
endpoint ``tenant_id`` joined explicitly). Cross-tenant nodes/edges
cannot surface even if a future invariant violation introduced one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from meho_backplane.db.models import GraphEdge, GraphNode
from meho_backplane.topology.query import TopologyNodeListEntry, list_nodes
from meho_backplane.ui.auth.middleware import UISessionContext
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.templating import get_templates

__all__ = [
    "GRAPH_NODE_CAP",
    "render_graph",
]


#: Frontend-side render cap. Cytoscape comfortably handles ~1k nodes,
#: but legibility past 500 collapses to a hairball. Beyond this number
#: the page surfaces a truncation banner and routes the operator at
#: the T3 (#882) subgraph query. Pinned per Initiative #342 work item
#: #6 ("Performance discipline. ... v0.2 caps frontend-side rendering
#: at 500 nodes").
GRAPH_NODE_CAP: Final[int] = 500

#: Module-private alias for use inside ``_fetch_edges_for_nodes`` --
#: keeps the public-facing constant clean while letting the helper
#: keep a short local name.
_GRAPH_NODE_CAP = GRAPH_NODE_CAP


@dataclass(frozen=True)
class _GraphEdgeRow:
    """One edge the graph view renders.

    Frozen dataclass because the rows are immutable projections of the
    query result. The Pydantic :class:`TopologyEdge` (in the substrate
    schema module) carries a deep-frozen ``properties`` map that the
    graph view does not need -- the lighter dataclass keeps the per-row
    allocation small for the up-to-500-node ceiling.
    """

    id: uuid.UUID
    kind: str
    source: str
    from_id: uuid.UUID
    to_id: uuid.UUID


async def _fetch_edges_for_nodes(
    db_session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    node_ids: list[uuid.UUID],
) -> list[_GraphEdgeRow]:
    """Return the live edges with both endpoints in *node_ids*.

    Tenant-scoped on the edge AND on both endpoint joins
    (defence-in-depth, matching the T1 drawer ``_fetch_edges`` shape).
    Soft-deleted edges (``last_seen IS NULL``) are excluded so the
    view shows the live snapshot, not stale relationships.

    The endpoint filter (``IN node_ids``) keeps the returned set
    bounded by the table-cap ceiling: with ``len(node_ids) <= 500``
    the worst-case edge count is bounded by the per-tenant graph
    density, which the production tenants stay well under the
    in-memory ceiling for.
    """
    if not node_ids:
        return []

    from_alias = aliased(GraphNode)
    to_alias = aliased(GraphNode)
    stmt = (
        select(
            GraphEdge.id.label("edge_id"),
            GraphEdge.kind.label("edge_kind"),
            GraphEdge.source.label("edge_source"),
            GraphEdge.from_node_id.label("from_id"),
            GraphEdge.to_node_id.label("to_id"),
        )
        .join(from_alias, from_alias.id == GraphEdge.from_node_id)
        .join(to_alias, to_alias.id == GraphEdge.to_node_id)
        .where(
            GraphEdge.tenant_id == tenant_id,
            from_alias.tenant_id == tenant_id,
            to_alias.tenant_id == tenant_id,
            GraphEdge.last_seen.is_not(None),
            GraphEdge.from_node_id.in_(node_ids),
            GraphEdge.to_node_id.in_(node_ids),
        )
        .order_by(GraphEdge.id)
    )
    result = await db_session.execute(stmt)
    return [
        _GraphEdgeRow(
            id=row.edge_id,
            kind=row.edge_kind,
            source=row.edge_source,
            from_id=row.from_id,
            to_id=row.to_id,
        )
        for row in result.all()
    ]


def _node_to_cy_element(node: TopologyNodeListEntry) -> dict[str, object]:
    """Project one node to a Cytoscape element JSON object.

    Cytoscape element shape: ``{"data": {"id": str, ...}, "group": "nodes"}``
    (see https://js.cytoscape.org/#notation/elements-json). ``id`` must
    be a string for Cytoscape's selector engine; the UUID renders via
    ``str(uuid)``. ``kind`` doubles as the class for per-kind styling
    in the template (square = host, ellipse = vm, etc.).
    """
    return {
        "group": "nodes",
        "data": {
            "id": str(node.id),
            "name": node.name,
            "kind": node.kind,
        },
        "classes": f"kind-{node.kind}",
    }


def _edge_to_cy_element(edge: _GraphEdgeRow) -> dict[str, object]:
    """Project one edge to a Cytoscape element JSON object.

    Cytoscape edge data needs ``source`` + ``target`` plus an ``id``
    distinct from any node id (using the edge's own UUID keeps it
    unambiguous). ``kind`` rides along so the template can label
    edges by relationship type.
    """
    return {
        "group": "edges",
        "data": {
            "id": str(edge.id),
            "source": str(edge.from_id),
            "target": str(edge.to_id),
            "kind": edge.kind,
        },
        "classes": f"kind-{edge.kind}",
    }


def _build_elements(
    nodes: list[TopologyNodeListEntry],
    edges: list[_GraphEdgeRow],
) -> list[dict[str, object]]:
    """Build the Cytoscape elements bag from nodes + edges.

    The list shape matches ``cytoscape({ elements: [...] })`` (see
    https://js.cytoscape.org/#init-opts/elements); the init script
    passes it straight in.
    """
    elements: list[dict[str, object]] = [_node_to_cy_element(n) for n in nodes]
    elements.extend(_edge_to_cy_element(e) for e in edges)
    return elements


def _build_template_context(
    *,
    elements: list[dict[str, object]],
    node_count: int,
    edge_count: int,
    truncated: bool,
    kind: str | None,
    name_contains: str | None,
    selected_id: uuid.UUID | None,
    csrf_token: str,
) -> dict[str, object]:
    """Assemble the Jinja2 context dict the graph template renders against.

    Pulled out of :func:`render_graph` so the rendering function stays
    inside the code-quality function-size budget (the docstring +
    context literal pushed it over). The shape is documented inline
    -- every key is consumed by ``topology/graph.html``.
    """
    return {
        "page_title": "Topology",
        "active_surface": "topology",
        "kind_filter": kind or "",
        "name_filter": name_contains or "",
        "csrf_token": csrf_token,
        "elements": elements,
        "node_count": node_count,
        "edge_count": edge_count,
        "graph_node_cap": GRAPH_NODE_CAP,
        "truncated": truncated,
        # ``selected_id`` is the cross-link payload: when the operator
        # clicks a table row's "Show in graph" button (or arrived via
        # ``?view=graph&selected=<id>`` from any source), the init
        # script centers + selects the matching Cytoscape node.
        # ``None`` surfaces to the template as an empty string --
        # Jinja's ``StrictUndefined`` env requires the key present.
        "selected_id": str(selected_id) if selected_id is not None else "",
        # ``ready=False`` so ``base.html``'s footer pill stays the
        # same colour as the table surface (the dashboard owns the
        # readiness signal).
        "ready": False,
    }


async def render_graph(
    request: object,
    *,
    session_ctx: UISessionContext,
    db_session: AsyncSession,
    kind: str | None,
    name_contains: str | None,
    selected_id: uuid.UUID | None,
) -> HTMLResponse:
    """Render the ``?view=graph`` Cytoscape page.

    The route at :mod:`meho_backplane.ui.routes.topology.table`
    branches on ``?view=`` and calls this function when ``graph`` is
    selected. The signature accepts the already-validated query
    params from the caller so the validation surface stays in the one
    place FastAPI sees -- the route definition.

    The 500-node cap is enforced here (``limit=GRAPH_NODE_CAP``); a
    returned list at the cap drives the truncation banner in the
    template via ``truncated=True``. ``selected_id`` round-trips the
    cross-link target from the table surface's ``?selected=`` param.
    """
    nodes = await list_nodes(
        db_session,
        session_ctx.tenant_id,
        kind=kind,
        name_contains=name_contains,
        sort="name",
        direction="asc",
        limit=GRAPH_NODE_CAP,
    )
    truncated = len(nodes) >= GRAPH_NODE_CAP
    edges = await _fetch_edges_for_nodes(
        db_session,
        tenant_id=session_ctx.tenant_id,
        node_ids=[node.id for node in nodes],
    )
    elements = _build_elements(nodes, edges)
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context = _build_template_context(
        elements=elements,
        node_count=len(nodes),
        edge_count=len(edges),
        truncated=truncated,
        kind=kind,
        name_contains=name_contains,
        selected_id=selected_id,
        csrf_token=csrf_token,
    )
    response = get_templates().TemplateResponse(
        request,  # type: ignore[arg-type]
        "topology/graph.html",
        context,
    )
    # Same CSRF posture as the table surface (T1 / #880).
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )
    return response
