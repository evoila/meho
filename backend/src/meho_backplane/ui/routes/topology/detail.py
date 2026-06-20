# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /ui/topology/node/{node_id}`` -- the per-node detail drawer.

Initiative #342 (G10.5 Topology UI), Task #880 (G10.5-T1) work item
#3. Renders the right-hand drawer pane the table row "view" buttons
target via ``hx-get`` / ``hx-target="#node-drawer"``. The drawer
shows:

* **Node properties** -- ``id``, ``kind``, ``name``, ``target_id``
  (when populated), ``first_seen`` / ``last_seen`` timestamps,
  ``discovered_by`` source, plus the JSON properties bag.
* **Incoming + outgoing edges** -- the immediate neighbours, one
  line per edge. Sourced from
  :func:`meho_backplane.topology.query.list_edges` with the
  ``from_ref`` / ``to_ref`` filters wired to the node's name.
* **Recent operations on the node** -- the last few ``audit_log``
  rows where ``target_id`` matches the node's ``target_id`` (only
  populated for ``target``-kind nodes -- inner graph nodes carry
  no target id and therefore have no audit-trail surface). Filtered
  by ``tenant_id`` and ordered ``occurred_at DESC``.
* **"Show dependents" link** -- an ``hx-get`` to the future T3
  graph-view route. The link is rendered today (so the URL contract
  surfaces to operators as soon as T1 ships); T3 (#882) wires the
  handler. The link does not navigate yet -- it carries the URL the
  graph view will accept.

Tenant scoping is enforced at every layer:

* The substrate ``list_edges`` and the local ``audit_log`` query
  both take ``tenant_id`` from the session-bound
  :class:`UISessionContext` -- never from a query param.
* The node-id resolution itself starts with ``graph_node.tenant_id
  = :tenant_id`` so a UUID belonging to another tenant returns 404
  rather than rendering that tenant's data.

Returns:

* **200 + drawer fragment** -- node found in the caller's tenant.
* **404** -- node id does not exist in this tenant (or exists only
  in another tenant; the boundary is opaque). Renders a small
  "node not found" fragment the HTMX swap displays inside
  ``#node-drawer``.

The route is HTMX-only by design (no full-page render): the drawer
is meaningful only inside the table view. A direct browser nav to
``/ui/topology/node/<id>`` returns a bare drawer fragment with no
``base.html`` chrome; operators reach the surface via the table
view's row buttons.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from meho_backplane.db.engine import get_raw_session
from meho_backplane.db.models import AuditLog, GraphEdge, GraphNode
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.csrf import mint_csrf_token
from meho_backplane.ui.routes.approvals.render import set_csrf_cookie
from meho_backplane.ui.routes.connectors.operator import OperatorRoleProbe, resolve_role_probe
from meho_backplane.ui.templating import get_templates

__all__ = ["build_detail_router"]


@dataclass(frozen=True)
class _EdgeEndpointRow:
    """Compact endpoint shape the drawer template renders.

    Mirrors :class:`meho_backplane.topology.schemas.TopologyEdgeEndpoint`
    on the field level (``id`` / ``kind`` / ``name``) but is a plain
    frozen dataclass rather than a Pydantic model -- the drawer is a
    UI-internal consumer, not part of the public REST surface.
    """

    id: uuid.UUID
    kind: str
    name: str


@dataclass(frozen=True)
class _EdgeRow:
    """One row of the per-node neighbour list rendered in the drawer.

    Carries the edge's ``kind`` + ``source`` plus the two endpoints
    by id/kind/name -- everything the drawer template needs without
    pulling :func:`meho_backplane.topology.query.list_edges`, whose
    flat-listing SQL relies on PostgreSQL JSONB functions (``jsonb_typeof``
    / ``jsonb_array_length`` for the ``conflicts_only`` predicate) that
    SQLite -- the dialect the chassis unit-test fixture uses -- does not
    provide. Building the neighbour list via plain SQLAlchemy ORM keeps
    the substrate dialect-portable; the conflict surface is out of scope
    for the drawer (operators recover from wrong annotations through
    the CLI / REST verbs G9.2 ships, not from the read-only drawer).
    """

    id: uuid.UUID
    kind: str
    source: str
    from_endpoint: _EdgeEndpointRow
    to_endpoint: _EdgeEndpointRow


#: Number of recent audit rows the drawer surfaces per node. Sized
#: so the drawer renders without scrolling for the typical inspection
#: question ("what changed here recently?"); operators who need the
#: full history walk hand off to the G8.1 audit-query CLI / API.
_RECENT_OPS_LIMIT = 10

#: Edge-listing per-direction cap. The drawer shows the immediate
#: neighbours, not the full closure (the graph view in T2 visualises
#: the closure). 50 per direction is a sensible eyeball-scan bound;
#: operators with denser graphs reach for the dependents / path
#: query overlays T3 ships.
_EDGE_LIMIT = 50


async def _fetch_node(
    db_session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    node_id: uuid.UUID,
) -> GraphNode | None:
    """Resolve ``(tenant_id, node_id)`` to a ``graph_node`` row.

    Returns ``None`` when no row matches. Cross-tenant ids surface
    identically -- the tenant boundary is opaque to the caller, per
    the same contract :func:`unannotate_edge` uses for its
    "edge_not_found" branch (the substrate refuses to leak the
    existence of rows in another tenant).
    """
    stmt = select(GraphNode).where(
        GraphNode.tenant_id == tenant_id,
        GraphNode.id == node_id,
    )
    result = await db_session.execute(stmt)
    return result.scalar_one_or_none()


async def _fetch_edges(
    db_session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    node_id: uuid.UUID,
    direction: str,
    limit: int,
) -> list[_EdgeRow]:
    """Return the immediate neighbour edges in *direction*.

    ``direction`` is ``"out"`` for edges leaving the node
    (``from_node_id = node_id``) or ``"in"`` for edges arriving at
    it (``to_node_id = node_id``). Soft-deleted edges
    (``last_seen IS NULL``) are excluded -- the drawer should show
    a live snapshot, not stale relationships.

    The query joins both endpoint nodes explicitly via aliases so the
    result carries the human-readable ``(kind, name)`` for each
    endpoint without an N+1 lookup. The tenant boundary is enforced
    explicitly on the edge (``GraphEdge.tenant_id == tenant_id``) AND
    on both joined endpoints (``from_alias.tenant_id`` /
    ``to_alias.tenant_id == tenant_id``) -- defense in depth, so a
    stray cross-tenant endpoint row could never surface here even if
    the refresh service's ``(tenant_id, kind, name)`` invariant were
    ever violated.
    """
    if direction not in ("in", "out"):
        raise ValueError(f"direction must be 'in' or 'out'; got {direction!r}")

    # Aliased endpoint joins so one statement carries both endpoints'
    # ``(id, kind, name)`` projections.
    from_alias = aliased(GraphNode)
    to_alias = aliased(GraphNode)

    stmt = (
        select(
            GraphEdge.id.label("edge_id"),
            GraphEdge.kind.label("edge_kind"),
            GraphEdge.source.label("edge_source"),
            GraphEdge.last_seen.label("edge_last_seen"),
            from_alias.id.label("from_id"),
            from_alias.kind.label("from_kind"),
            from_alias.name.label("from_name"),
            to_alias.id.label("to_id"),
            to_alias.kind.label("to_kind"),
            to_alias.name.label("to_name"),
        )
        .join(from_alias, from_alias.id == GraphEdge.from_node_id)
        .join(to_alias, to_alias.id == GraphEdge.to_node_id)
        .where(
            GraphEdge.tenant_id == tenant_id,
            from_alias.tenant_id == tenant_id,
            to_alias.tenant_id == tenant_id,
            GraphEdge.last_seen.is_not(None),
        )
        .order_by(GraphEdge.last_seen.desc(), GraphEdge.id)
        .limit(limit)
    )
    if direction == "out":
        stmt = stmt.where(GraphEdge.from_node_id == node_id)
    else:
        stmt = stmt.where(GraphEdge.to_node_id == node_id)

    result = await db_session.execute(stmt)
    return [
        _EdgeRow(
            id=row.edge_id,
            kind=row.edge_kind,
            source=row.edge_source,
            from_endpoint=_EdgeEndpointRow(
                id=row.from_id,
                kind=row.from_kind,
                name=row.from_name,
            ),
            to_endpoint=_EdgeEndpointRow(
                id=row.to_id,
                kind=row.to_kind,
                name=row.to_name,
            ),
        )
        for row in result.all()
    ]


async def _fetch_recent_ops(
    db_session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    target_id: uuid.UUID | None,
    limit: int = _RECENT_OPS_LIMIT,
) -> list[AuditLog]:
    """Return the most recent ``audit_log`` rows on the node's target.

    Only ``target``-kind graph nodes carry a ``target_id`` (the FK
    to ``targets.id``); inner graph nodes (VMs, pods, datastores)
    have ``target_id = NULL`` and therefore no audit-trail entries
    of their own. The drawer surfaces an empty list rather than an
    error for those rows -- the absence of audit trail is part of
    the inventory's accurate shape.

    The query is scoped to the operator's tenant on both
    ``audit_log.tenant_id`` and (implicitly, via the ``target_id``
    filter being NULL-safe). A ``target_id`` cannot be passed
    through this route from another tenant: the caller only ever
    reaches this helper via the ``_fetch_node`` resolver which
    already pinned ``tenant_id``; the ``target_id`` it carries is
    guaranteed to be tenant-scoped (cross-tenant FK is impossible
    by ``targets.tenant_id`` invariant).
    """
    if target_id is None:
        return []
    stmt = (
        select(AuditLog)
        .where(
            AuditLog.tenant_id == tenant_id,
            AuditLog.target_id == target_id,
        )
        .order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())
        .limit(limit)
    )
    result = await db_session.execute(stmt)
    return list(result.scalars().all())


#: Module-level :class:`fastapi.Depends` closures -- ruff B008 guard.
_require_ui_session_dep = Depends(require_ui_session)
_get_raw_session_dep = Depends(get_raw_session)
_resolve_role_probe_dep = Depends(resolve_role_probe)


def _build_drawer_context(
    *,
    node: GraphNode,
    outgoing: list[_EdgeRow],
    incoming: list[_EdgeRow],
    recent_ops: list[AuditLog],
    is_tenant_admin: bool,
    csrf_token: str,
) -> dict[str, object]:
    """Assemble the Jinja2 context dict the drawer template renders.

    Extracted so :func:`build_detail_router._handler` stays inside the
    code-quality function-size budget. The ``dependents_href`` is the
    "show dependents" link target -- T3 (#882) wired the handler; the
    URL contract is ``?from=<name>&from_kind=<kind>`` so the
    dependents subgraph overlay loads with the right anchor.

    ``is_tenant_admin`` drives the **soft-hide** of the curated-edge
    annotate / remove controls (Task #1953): an operator never sees the
    write affordances. The server-side authority is the
    ``/ui/topology/edges*`` routes' ``tenant_admin`` gate -- the hidden
    button is UX only; a forged POST still 403s. ``csrf_token`` is the
    double-submit token the annotate-modal-open ``hx-get`` and the
    per-edge remove ``hx-delete`` echo back so the CSRF middleware accepts
    them; the drawer render (re)sets the matching ``meho_csrf`` cookie.

    ``graph_node.name`` is unconstrained Text (connector-populated);
    a name containing ``&`` / ``?`` / ``#`` / ``+`` / ``%`` / space
    would silently corrupt the query string when interpolated raw.
    :func:`urllib.parse.quote` with ``safe=''`` percent-encodes every
    byte that is not in the unreserved set, including ``/`` and
    ``&`` -- the overlay decoder pairs with that on the way in.
    """
    return {
        "node": node,
        "outgoing_edges": outgoing,
        "incoming_edges": incoming,
        "recent_ops": recent_ops,
        "is_tenant_admin": is_tenant_admin,
        "csrf_token": csrf_token,
        "annotate_modal_href": "/ui/topology/edges/annotate",
        "dependents_href": (
            f"/ui/topology?view=graph"
            f"&from={quote(node.name, safe='')}"
            f"&from_kind={quote(node.kind, safe='')}"
        ),
        # Deep-link to this node's temporal history panel (Task #1955). The
        # ``kind`` disambiguates the bare name up-front so the history route
        # never has to fall back to its ambiguous-node banner for a node the
        # drawer already resolved. ``name`` / ``kind`` are unconstrained Text;
        # ``quote(safe='')`` percent-encodes the path segment + the query value.
        "history_href": (
            f"/ui/topology/history/{quote(node.name, safe='')}?kind={quote(node.kind, safe='')}"
        ),
    }


async def _render_node_detail(
    request: Request,
    node_id: uuid.UUID,
    *,
    session_ctx: UISessionContext,
    db_session: AsyncSession,
    role_probe: OperatorRoleProbe,
) -> HTMLResponse:
    """Resolve the node tenant-scoped and render the drawer fragment.

    Pulls the incoming + outgoing edges via the SQLite-portable ORM query
    and the recent ``audit_log`` rows, then renders ``topology/_drawer.html``
    with the full context. The ``role_probe`` (fail-soft ``tenant_admin``
    projection) drives the soft-hide of the curated-edge write controls; the
    drawer also mints + re-sets the ``meho_csrf`` cookie so an admin's
    annotate / remove request carries a valid double-submit token.

    Returns the not-found drawer fragment (HTTP 404) for a missing /
    cross-tenant id; HTMX swaps either response into ``#node-drawer``.
    """
    node = await _fetch_node(
        db_session,
        tenant_id=session_ctx.tenant_id,
        node_id=node_id,
    )
    if node is None:
        # 404 fragment -- HTMX swaps it into the drawer and the operator
        # sees the "not found" message without a full page reload. HTMX does
        # not auto-clear the swap target on 4xx by default, which is the
        # desired behaviour.
        return get_templates().TemplateResponse(
            request,
            "topology/_drawer_not_found.html",
            {"node_id": str(node_id)},
            status_code=404,
        )

    outgoing = await _fetch_edges(
        db_session,
        tenant_id=session_ctx.tenant_id,
        node_id=node.id,
        direction="out",
        limit=_EDGE_LIMIT,
    )
    incoming = await _fetch_edges(
        db_session,
        tenant_id=session_ctx.tenant_id,
        node_id=node.id,
        direction="in",
        limit=_EDGE_LIMIT,
    )
    recent_ops = await _fetch_recent_ops(
        db_session,
        tenant_id=session_ctx.tenant_id,
        target_id=node.target_id,
    )

    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context = _build_drawer_context(
        node=node,
        outgoing=outgoing,
        incoming=incoming,
        recent_ops=recent_ops,
        is_tenant_admin=role_probe.is_tenant_admin,
        csrf_token=csrf_token,
    )
    response = get_templates().TemplateResponse(request, "topology/_drawer.html", context)
    # Re-set the matching ``meho_csrf`` cookie so the drawer's annotate /
    # remove controls' echoed token lines up after the HTMX swap (the
    # cookie-desync class the approvals modal also guards against).
    set_csrf_cookie(response, csrf_token)
    return response


def build_detail_router() -> APIRouter:
    """Construct the topology-node-detail :class:`APIRouter`.

    Registers the single ``GET /ui/topology/node/{node_id}`` route.
    Returns the drawer fragment on success and a small 404 fragment
    on a missing / cross-tenant id; both responses are designed for
    HTMX swap into ``#node-drawer``.
    """
    router = APIRouter(tags=["ui-topology"])

    async def _handler(
        request: Request,
        node_id: uuid.UUID,
        session_ctx: UISessionContext = _require_ui_session_dep,
        db_session: AsyncSession = _get_raw_session_dep,
        role_probe: OperatorRoleProbe = _resolve_role_probe_dep,
    ) -> HTMLResponse:
        """``GET /ui/topology/node/{node_id}`` -- delegates to the renderer."""
        return await _render_node_detail(
            request,
            node_id,
            session_ctx=session_ctx,
            db_session=db_session,
            role_probe=role_probe,
        )

    router.add_api_route(
        "/ui/topology/node/{node_id}",
        _handler,
        methods=["GET"],
        name="ui_topology_node_detail",
        response_class=HTMLResponse,
        responses={
            404: {
                "description": (
                    "Node id does not exist in this tenant (or exists only "
                    "for another tenant). Returns the not-found drawer fragment."
                ),
                "content": {"text/html": {}},
            },
        },
    )
    return router
