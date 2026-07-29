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
from enum import StrEnum
from typing import Final

from fastapi import Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from meho_backplane.db.models import WELL_KNOWN_NODE_KINDS, GraphEdge, GraphNode
from meho_backplane.topology.query import TopologyNodeListEntry, list_nodes
from meho_backplane.ui.auth.middleware import UISessionContext
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.routes.topology.path_queries import fetch_path_subgraph
from meho_backplane.ui.routes.topology.queries import (
    DEFAULT_OVERLAY_DEPTH,
    DEFAULT_PATH_MAX_HOPS,
    MAX_OVERLAY_DEPTH,
    MAX_PATH_MAX_HOPS,
    AmbiguousNodeError,
    NodeNotFoundError,
    PathSubgraphResult,
    SubgraphEdgeRow,
    SubgraphNodeRow,
    SubgraphResult,
    fetch_dependencies_subgraph,
    fetch_dependents_subgraph,
)
from meho_backplane.ui.templating import get_templates

__all__ = [
    "GRAPH_NODE_CAP",
    "OverlayDirection",
    "render_graph",
    "render_graph_fragment",
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


class OverlayDirection(StrEnum):
    """Closed enum of traversal directions for the ``?from=`` overlay.

    Exposed on the route as the ``?direction=`` query param. ``dependents``
    walks edges INTO the root (reverse traversal -- "what depends on
    me"); ``dependencies`` walks edges OUT of the root (forward
    traversal -- "what I depend on"). The default ``dependents``
    matches the operator's typical question on first arrival from the
    drawer's "Show dependents" link.
    """

    DEPENDENTS = "dependents"
    DEPENDENCIES = "dependencies"


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


def _subgraph_node_to_cy_element(
    node: SubgraphNodeRow, *, root_id: uuid.UUID | None = None
) -> dict[str, object]:
    """Project an overlay subgraph node to a Cytoscape element JSON object.

    Mirrors :func:`_node_to_cy_element`; the only difference is that
    overlay nodes also carry a ``root`` class on the anchor node so
    the stylesheet can render it with a thicker border. ``root_id``
    is optional -- the path overlay does not have a single root.
    """
    classes = f"kind-{node.kind}"
    if root_id is not None and node.id == root_id:
        classes = f"{classes} root"
    return {
        "group": "nodes",
        "data": {
            "id": str(node.id),
            "name": node.name,
            "kind": node.kind,
        },
        "classes": classes,
    }


def _subgraph_edge_to_cy_element(
    edge: SubgraphEdgeRow, *, highlighted: bool = False
) -> dict[str, object]:
    """Project an overlay subgraph edge to a Cytoscape element JSON object.

    ``highlighted=True`` adds the ``highlight`` class -- the path
    overlay uses this on the path's own edges so the Cytoscape
    stylesheet renders them with a distinct colour. Plain edges
    (context in a path overlay, all edges in a dependents overlay)
    have ``highlight`` absent.
    """
    classes = f"kind-{edge.kind}"
    if highlighted:
        classes = f"{classes} highlight"
    return {
        "group": "edges",
        "data": {
            "id": str(edge.id),
            "source": str(edge.from_id),
            "target": str(edge.to_id),
            "kind": edge.kind,
        },
        "classes": classes,
    }


def _build_subgraph_elements(
    result: SubgraphResult,
) -> list[dict[str, object]]:
    """Build the Cytoscape elements bag for a dependents/dependencies overlay."""
    elements: list[dict[str, object]] = [
        _subgraph_node_to_cy_element(n, root_id=result.root_id) for n in result.nodes
    ]
    elements.extend(_subgraph_edge_to_cy_element(e) for e in result.edges)
    return elements


def _build_path_elements(
    result: PathSubgraphResult,
) -> list[dict[str, object]]:
    """Build the Cytoscape elements bag for a path overlay.

    Every path edge gets the ``highlight`` class so the stylesheet
    renders it in the highlighted colour; every path node also gets
    a small position marker (kept in ``classes`` so the per-kind
    palette is preserved).
    """
    elements: list[dict[str, object]] = [_subgraph_node_to_cy_element(n) for n in result.nodes]
    elements.extend(
        _subgraph_edge_to_cy_element(edge, highlighted=(edge.id in result.highlighted_edge_ids))
        for edge in result.edges
    )
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

    ``node_kind_options`` mirrors the T1 (#880) ``table.py`` pattern of
    sourcing the kind filter dropdown from the well-known set
    (:data:`meho_backplane.db.models.WELL_KNOWN_NODE_KINDS`) rather than
    hard-coding the vocabulary in the template. The kind space is open
    (T1 #2534); nodes of a novel kind still render — the dropdown just
    lists the documented core set (surfacing novel kinds in the filter
    is future console work).
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
        # Sourced from the well-known set -- single source of truth
        # shared with the T1 tabular surface
        # (``table.py._node_kind_options``).
        "node_kind_options": sorted(WELL_KNOWN_NODE_KINDS),
        # ``selected_id`` is the cross-link payload: when the operator
        # clicks a table row's "Show in graph" button (or arrived via
        # ``?view=graph&selected=<id>`` from any source), the init
        # script centers + selects the matching Cytoscape node.
        # ``None`` surfaces to the template as an empty string --
        # Jinja's ``StrictUndefined`` env requires the key present.
        "selected_id": str(selected_id) if selected_id is not None else "",
    }


def _build_refresh_url(
    *,
    overlay_mode: str,
    root_name: str | None,
    root_kind: str | None,
    direction: OverlayDirection | None,
    depth: int | None,
    to_name: str | None,
    to_kind: str | None,
    kind: str | None,
    name_contains: str | None,
    selected_id: uuid.UUID | None,
) -> str:
    """Build the URL HTMX polls every 30s to re-fetch this view.

    Mirrors the URL the operator copy/pasted into the browser bar so
    the polling refresh stays on the same overlay. ``view=graph`` is
    pinned because the polling refresh only fires on the graph
    branch. The encoding uses ``urllib.parse.quote`` (``safe=''``)
    to match the same posture the detail drawer's
    ``dependents_href`` uses.
    """
    # Local import keeps the helper near the consumer; avoids
    # pulling the urllib name into the module-level namespace where
    # the rest of the module does not use it.
    from urllib.parse import urlencode

    params: list[tuple[str, str]] = [("view", "graph")]
    if overlay_mode in ("dependents", "dependencies"):
        # Subgraph overlay -- carry the anchor + direction + depth.
        if root_name is not None:
            params.append(("from", root_name))
        if root_kind:
            params.append(("from_kind", root_kind))
        if direction is not None:
            params.append(("direction", direction.value))
        if depth is not None:
            params.append(("depth", str(depth)))
    elif overlay_mode == "path":
        # Path overlay -- carry both endpoints.
        if root_name is not None:
            params.append(("from", root_name))
        if root_kind:
            params.append(("from_kind", root_kind))
        if to_name:
            params.append(("to", to_name))
        if to_kind:
            params.append(("to_kind", to_kind))
    else:
        # Full inventory -- carry the kind + name filters + selection.
        if kind:
            params.append(("kind", kind))
        if name_contains:
            params.append(("q", name_contains))
        if selected_id is not None:
            params.append(("selected", str(selected_id)))
    return f"/ui/topology?{urlencode(params)}"


def _is_htmx_request(request: Request) -> bool:
    """Return ``True`` when the request was issued by HTMX.

    The graph view uses the same case-insensitive HX-Request header
    check as the table surface to distinguish a normal browser nav
    (full page) from an HTMX polling refresh (data-island fragment).
    HTMX 2 sets ``HX-Request: true`` on every directive-driven fetch
    (see https://htmx.org/reference/#request_headers).
    """
    return request.headers.get("hx-request", "").lower() == "true"


def _build_overlay_template_context(
    *,
    elements: list[dict[str, object]],
    node_count: int,
    edge_count: int,
    truncated: bool,
    overlay_mode: str,
    root_name: str,
    root_kind: str | None,
    direction: OverlayDirection | None,
    depth: int | None,
    to_name: str | None,
    to_kind: str | None,
    path_found: bool,
    total_hops: int | None,
    path_node_ids: tuple[uuid.UUID, ...],
    csrf_token: str,
) -> dict[str, object]:
    """Assemble the Jinja2 context for an overlay graph render.

    Mirrors :func:`_build_template_context` for the standard full view
    but extends it with overlay-specific bindings:

    * ``overlay_mode`` -- ``"dependents"`` / ``"dependencies"`` / ``"path"``
      so the template surfaces the active mode in the header.
    * ``root_name`` + ``root_kind`` -- echoed for the title.
    * ``direction`` -- which subgraph the dependents/dependencies
      branch rendered.
    * ``depth`` -- the active depth for the dependents/dependencies
      branch.
    * ``to_name`` + ``to_kind`` -- the path target for the path mode.
    * ``path_found`` -- whether a path was located within ``max_hops``.
    * ``total_hops`` -- the path length, ``None`` for not-found.
    * ``path_node_ids`` -- the ordered path ids; the JS controller
      reads this to highlight the path nodes (matching the
      ``highlight`` class on path edges).
    """
    return {
        "page_title": "Topology",
        "active_surface": "topology",
        # No kind/name filters on an overlay -- the URL contract is
        # ``?from=...`` (anchor-driven), not ``?kind=...&q=...``
        # (inventory-driven).
        "kind_filter": "",
        "name_filter": "",
        "csrf_token": csrf_token,
        "elements": elements,
        "node_count": node_count,
        "edge_count": edge_count,
        "graph_node_cap": GRAPH_NODE_CAP,
        "truncated": truncated,
        "node_kind_options": sorted(WELL_KNOWN_NODE_KINDS),
        # No table cross-link target in overlay mode -- empty string
        # so Jinja's ``StrictUndefined`` env does not raise on the
        # template read.
        "selected_id": "",
        # Overlay-specific bindings the template renders only when
        # ``overlay_mode`` is set.
        "overlay_mode": overlay_mode,
        "overlay_root_name": root_name,
        "overlay_root_kind": root_kind or "",
        "overlay_direction": direction.value if direction is not None else "",
        "overlay_depth": depth if depth is not None else "",
        "overlay_to_name": to_name or "",
        "overlay_to_kind": to_kind or "",
        "overlay_path_found": path_found,
        "overlay_total_hops": total_hops if total_hops is not None else "",
        # JSON-serialisable list for the data island.
        "overlay_path_node_ids": [str(nid) for nid in path_node_ids],
        # The polling-refresh target URL the HTMX wrapper hits every
        # 30s (G10.5-T3 (#882) work-item #3).
        "refresh_url": _build_refresh_url(
            overlay_mode=overlay_mode,
            root_name=root_name,
            root_kind=root_kind,
            direction=direction,
            depth=depth,
            to_name=to_name,
            to_kind=to_kind,
            kind=None,
            name_contains=None,
            selected_id=None,
        ),
    }


def _build_full_template_context(
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
    """Wrap :func:`_build_template_context` with the full-view overlay
    bindings set to their inactive defaults.

    The template reads ``overlay_mode`` to branch on what to render;
    the inactive defaults (empty string for ``overlay_mode``) mean the
    full inventory view renders its existing chrome (the kind +
    name filters), and the overlay-only chrome stays hidden.
    """
    base = _build_template_context(
        elements=elements,
        node_count=node_count,
        edge_count=edge_count,
        truncated=truncated,
        kind=kind,
        name_contains=name_contains,
        selected_id=selected_id,
        csrf_token=csrf_token,
    )
    # Inactive defaults for the overlay-only bindings -- StrictUndefined
    # requires every key the template reads to be present.
    base.update(
        {
            "overlay_mode": "",
            "overlay_root_name": "",
            "overlay_root_kind": "",
            "overlay_direction": "",
            "overlay_depth": "",
            "overlay_to_name": "",
            "overlay_to_kind": "",
            "overlay_path_found": False,
            "overlay_total_hops": "",
            "overlay_path_node_ids": [],
            # Polling-refresh URL: on the full inventory branch the
            # refresh re-fetches the inventory with the same kind +
            # name filters and the cross-link selection preserved
            # (so a graph -> table -> graph round-trip with a node
            # selected does not lose the selection when polling fires).
            "refresh_url": _build_refresh_url(
                overlay_mode="",
                root_name=None,
                root_kind=None,
                direction=None,
                depth=None,
                to_name=None,
                to_kind=None,
                kind=kind,
                name_contains=name_contains,
                selected_id=selected_id,
            ),
        }
    )
    return base


def _set_csrf_cookie(response: HTMLResponse, csrf_token: str) -> None:
    """Pin the CSRF cookie on the response.

    Same posture as the T1 tabular surface and the T2 full-graph
    surface -- the cookie is intentionally not ``HttpOnly`` so HTMX
    can read it to populate ``X-CSRF-Token`` on subsequent state-
    changing actions.
    """
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )


async def _render_full_inventory(
    request: Request,
    *,
    session_ctx: UISessionContext,
    db_session: AsyncSession,
    kind: str | None,
    name_contains: str | None,
    selected_id: uuid.UUID | None,
    htmx_fragment: bool,
) -> HTMLResponse:
    """Render the full-inventory graph view (no ``?from=``).

    Extracted from :func:`render_graph` so that function stays a
    thin dispatcher; the full-inventory path was the original T2
    (#881) shipping body.
    """
    nodes = await list_nodes(
        db_session,
        session_ctx.tenant_id,
        kind=kind,
        name_contains=name_contains,
        sort="name",
        direction="asc",
        limit=GRAPH_NODE_CAP + 1,
    )
    truncated = len(nodes) > GRAPH_NODE_CAP
    if truncated:
        nodes = nodes[:GRAPH_NODE_CAP]
    edges = await _fetch_edges_for_nodes(
        db_session,
        tenant_id=session_ctx.tenant_id,
        node_ids=[node.id for node in nodes],
    )
    elements = _build_elements(nodes, edges)
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context = _build_full_template_context(
        elements=elements,
        node_count=len(nodes),
        edge_count=len(edges),
        truncated=truncated,
        kind=kind,
        name_contains=name_contains,
        selected_id=selected_id,
        csrf_token=csrf_token,
    )
    template_name = "topology/_graph_data_island.html" if htmx_fragment else "topology/graph.html"
    response = get_templates().TemplateResponse(request, template_name, context)
    _set_csrf_cookie(response, csrf_token)
    return response


async def _render_dependents_or_dependencies_overlay(
    request: Request,
    *,
    session_ctx: UISessionContext,
    db_session: AsyncSession,
    from_name: str,
    from_kind: str | None,
    direction: OverlayDirection,
    depth: int,
    htmx_fragment: bool,
) -> HTMLResponse:
    """Render the dependents (reverse) or dependencies (forward) overlay."""
    fetcher = (
        fetch_dependents_subgraph
        if direction == OverlayDirection.DEPENDENTS
        else fetch_dependencies_subgraph
    )
    try:
        result: SubgraphResult = await fetcher(
            db_session,
            tenant_id=session_ctx.tenant_id,
            name=from_name,
            kind=from_kind,
            depth=depth,
            max_nodes=GRAPH_NODE_CAP,
        )
    except NodeNotFoundError as exc:
        return _render_overlay_error(
            request,
            session_ctx=session_ctx,
            status_code=404,
            message=str(exc),
            htmx_fragment=htmx_fragment,
        )
    except AmbiguousNodeError as exc:
        return _render_overlay_error(
            request,
            session_ctx=session_ctx,
            status_code=409,
            message=str(exc),
            htmx_fragment=htmx_fragment,
        )

    elements = _build_subgraph_elements(result)
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context = _build_overlay_template_context(
        elements=elements,
        node_count=len(result.nodes),
        edge_count=len(result.edges),
        truncated=result.truncated,
        overlay_mode=direction.value,
        root_name=result.root_name,
        root_kind=result.root_kind,
        direction=direction,
        depth=depth,
        to_name=None,
        to_kind=None,
        path_found=False,
        total_hops=None,
        path_node_ids=(),
        csrf_token=csrf_token,
    )
    template_name = "topology/_graph_data_island.html" if htmx_fragment else "topology/graph.html"
    response = get_templates().TemplateResponse(request, template_name, context)
    _set_csrf_cookie(response, csrf_token)
    return response


async def _render_path_overlay(
    request: Request,
    *,
    session_ctx: UISessionContext,
    db_session: AsyncSession,
    from_name: str,
    from_kind: str | None,
    to_name: str,
    to_kind: str | None,
    max_hops: int,
    htmx_fragment: bool,
) -> HTMLResponse:
    """Render the shortest-path overlay between two named nodes."""
    try:
        result: PathSubgraphResult = await fetch_path_subgraph(
            db_session,
            tenant_id=session_ctx.tenant_id,
            from_name=from_name,
            to_name=to_name,
            from_kind=from_kind,
            to_kind=to_kind,
            max_hops=max_hops,
        )
    except NodeNotFoundError as exc:
        return _render_overlay_error(
            request,
            session_ctx=session_ctx,
            status_code=404,
            message=str(exc),
            htmx_fragment=htmx_fragment,
        )
    except AmbiguousNodeError as exc:
        return _render_overlay_error(
            request,
            session_ctx=session_ctx,
            status_code=409,
            message=str(exc),
            htmx_fragment=htmx_fragment,
        )

    elements = _build_path_elements(result)
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context = _build_overlay_template_context(
        elements=elements,
        node_count=len(result.nodes),
        edge_count=len(result.edges),
        truncated=result.truncated,
        overlay_mode="path",
        root_name=from_name,
        root_kind=from_kind,
        direction=None,
        depth=None,
        to_name=to_name,
        to_kind=to_kind,
        path_found=result.total_hops is not None,
        total_hops=result.total_hops,
        path_node_ids=result.path_node_ids,
        csrf_token=csrf_token,
    )
    template_name = "topology/_graph_data_island.html" if htmx_fragment else "topology/graph.html"
    response = get_templates().TemplateResponse(request, template_name, context)
    _set_csrf_cookie(response, csrf_token)
    return response


def _render_overlay_error(
    request: Request,
    *,
    session_ctx: UISessionContext,
    status_code: int,
    message: str,
    htmx_fragment: bool,
) -> HTMLResponse:
    """Render the not-found / ambiguous-name error fragment.

    ``status_code`` carries the HTTP semantics (404 unknown name,
    409 ambiguous name); the body is a small panel inside the
    standard chrome so the operator stays in context (rather than
    bouncing to a global error page).

    The fragment-mode branch returns just the inline error block so
    a polling HTMX swap surfaces the error message without rewriting
    the surrounding page chrome.
    """
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    template_name = (
        "topology/_graph_overlay_error_fragment.html"
        if htmx_fragment
        else "topology/_graph_overlay_error.html"
    )
    context = {
        "page_title": "Topology",
        "active_surface": "topology",
        "csrf_token": csrf_token,
        "error_status": status_code,
        "error_message": message,
    }
    response = get_templates().TemplateResponse(
        request,
        template_name,
        context,
        status_code=status_code,
    )
    _set_csrf_cookie(response, csrf_token)
    return response


def _clamp_depth(depth: int | None) -> int:
    """Clamp the operator-supplied depth into ``[1, MAX_OVERLAY_DEPTH]``.

    ``None`` -> :data:`~meho_backplane.ui.routes.topology.queries.DEFAULT_OVERLAY_DEPTH`.
    Out-of-range values silently clamp; the route layer's pydantic
    validator constrains the HTTP boundary tighter (rejects values
    outside the range), but this defensive clamp keeps the helper
    safe when called from a future internal context (CLI/MCP/REPL)
    without the same validation.
    """
    if depth is None:
        return DEFAULT_OVERLAY_DEPTH
    if depth < 1:
        return 1
    if depth > MAX_OVERLAY_DEPTH:
        return MAX_OVERLAY_DEPTH
    return depth


def _clamp_max_hops(max_hops: int | None) -> int:
    """Clamp the operator-supplied path hop ceiling.

    Same posture as :func:`_clamp_depth`.
    """
    if max_hops is None:
        return DEFAULT_PATH_MAX_HOPS
    if max_hops < 1:
        return 1
    if max_hops > MAX_PATH_MAX_HOPS:
        return MAX_PATH_MAX_HOPS
    return max_hops


async def render_graph(
    request: Request,
    *,
    session_ctx: UISessionContext,
    db_session: AsyncSession,
    kind: str | None,
    name_contains: str | None,
    selected_id: uuid.UUID | None,
    from_name: str | None = None,
    from_kind: str | None = None,
    to_name: str | None = None,
    to_kind: str | None = None,
    direction: OverlayDirection | None = None,
    depth: int | None = None,
    max_hops: int | None = None,
) -> HTMLResponse:
    """Render the ``?view=graph`` Cytoscape surface (full or overlay).

    The route at :mod:`meho_backplane.ui.routes.topology.table`
    branches on ``?view=`` and calls this function when ``graph`` is
    selected. The signature accepts the already-validated query
    params from the caller so the validation surface stays in the one
    place FastAPI sees -- the route definition.

    Mode is decided here from the optional overlay query params:

    * ``from_name`` + ``to_name`` both set -> path overlay (G10.5-T3
      #882 work-item #2): the shortest unweighted path between two
      named nodes with edges highlighted.
    * ``from_name`` set, ``to_name`` unset -> dependents (default) or
      dependencies subgraph (G10.5-T3 work-item #1).
    * Neither set -> full inventory (T2 / #881 shipping body).

    The HX-Request header surfaces the polling-refresh branch
    (G10.5-T3 work-item #3): the response is the data-island fragment
    only, swapped in by HTMX's ``every 30s`` trigger without
    rewriting the chrome.

    The 500-node cap (T2 / #881) is enforced on every branch via
    :data:`GRAPH_NODE_CAP`. ``selected_id`` round-trips the cross-
    link target on the full-inventory branch only -- overlays do not
    expose a separate selection state.
    """
    htmx_fragment = _is_htmx_request(request)

    # Path overlay: both endpoints set.
    if from_name is not None and to_name is not None:
        return await _render_path_overlay(
            request,
            session_ctx=session_ctx,
            db_session=db_session,
            from_name=from_name,
            from_kind=from_kind,
            to_name=to_name,
            to_kind=to_kind,
            max_hops=_clamp_max_hops(max_hops),
            htmx_fragment=htmx_fragment,
        )

    # Dependents / dependencies subgraph: ``from`` set, ``to`` unset.
    if from_name is not None:
        active_direction = direction or OverlayDirection.DEPENDENTS
        return await _render_dependents_or_dependencies_overlay(
            request,
            session_ctx=session_ctx,
            db_session=db_session,
            from_name=from_name,
            from_kind=from_kind,
            direction=active_direction,
            depth=_clamp_depth(depth),
            htmx_fragment=htmx_fragment,
        )

    # Full inventory (T2 / #881 shipping body).
    return await _render_full_inventory(
        request,
        session_ctx=session_ctx,
        db_session=db_session,
        kind=kind,
        name_contains=name_contains,
        selected_id=selected_id,
        htmx_fragment=htmx_fragment,
    )


# Backwards-compat alias for callers that prefer the explicit name
# (e.g. a future internal route handler).
render_graph_fragment = render_graph
