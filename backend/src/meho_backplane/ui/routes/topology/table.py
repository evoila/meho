# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /ui/topology`` -- the tenant topology tabular + graph surface.

Initiative #342 (G10.5 Topology UI), Task #880 (G10.5-T1) shipped the
tabular view; Task #881 (G10.5-T2) layered the Cytoscape graph view on
top of the same path via the ``?view=graph`` branch.

Three response shapes share one handler:

* **Tabular full page** (``?view=table`` or unset) -- a normal browser
  navigation returns the full ``topology/table.html`` page (extends
  ``base.html``; navbar, sidebar, filter bar, sort headers, table
  body, and the empty ``#node-drawer`` slot the per-node drawer
  swaps into).

* **Tabular HTMX fragment** (``?view=table`` or unset with
  ``HX-Request: true``) -- a sort / filter swap returns the
  ``topology/_table_rows.html`` partial swapped into the existing
  table without a full-page reload.

* **Graph full page** (``?view=graph``) -- returns the
  ``topology/graph.html`` page with a server-rendered Cytoscape.js
  island; elements + cross-link selection ride into the page as a
  ``<script type="application/json">`` data island the init script
  reads on load. The graph view does not have an HTMX-fragment
  variant: layout switches happen client-side via
  ``cy.layout({name}).run()``; filter / kind changes round-trip to
  the server (full-page reload) so the URL captures the active mode
  for copy/paste.

Tenant scoping is non-overrideable. Every call to
:func:`meho_backplane.topology.query.list_nodes` (table) and the
graph-route's edge-and-node fetch passes ``operator.tenant_id`` from
the session-bound :class:`UISessionContext`; no query parameter or
body field carries a tenant id. Another tenant's node never renders
-- the acceptance criterion is enforced at the substrate layer (the
listing SQL's first ``WHERE`` clause), not the template.

Sort column + direction defaults are the ``name`` column ascending
-- a stable, human-meaningful order. ``view=graph`` switches to the
Cytoscape view; ``view=table`` (or unset) keeps the tabular surface.
``selected`` (a UUID) cross-links between the two: a graph node's
tap arrives at ``?view=table&selected=<id>`` (the row scrolls into
view + highlights) and vice versa.

T3 (#882) URL contract::

    GET /ui/topology
        [?view=table|graph
         &sort=...&direction=...
         &kind=...&q=...
         &selected=<uuid>
         # G10.5-T3 graph overlays (view=graph only):
         &from=<name>[&from_kind=<kind>][&depth=N]
         [&direction=dependents|dependencies]
         &from=A&to=B[&from_kind=...&to_kind=...&max_hops=N]]

``direction`` is dual-purpose by branch:

* **Table branch** (default) -- the sort direction: ``asc`` (default)
  or ``desc``. Out-of-range -> 422.
* **Graph branch** with ``?from=<name>&to=`` unset -- the overlay
  direction: ``dependents`` (default) or ``dependencies``. Out-of-
  range silently defaults so a graph<->table toggle preserving
  ``direction=asc`` does not 422 on the graph side.
* **Graph branch** with ``?from=A&to=B`` -- ignored (a path has no
  direction).
* **Graph branch** with ``?from=`` unset -- ignored (the full-
  inventory view has no direction).

The dual-purpose contract keeps the URL surface aligned with the
issue #882 spec (``?direction=dependencies``) without forcing the
table-sort and graph-overlay senses into separate params, which
would have widened the OpenAPI footprint with no operator benefit.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import StringConstraints
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_raw_session
from meho_backplane.db.models import _GRAPH_NODE_KINDS
from meho_backplane.topology.query import list_nodes
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.query_filters import EMPTY_STR_TO_NONE
from meho_backplane.ui.routes.connectors.operator import resolve_role_probe
from meho_backplane.ui.routes.topology.graph import OverlayDirection, render_graph
from meho_backplane.ui.routes.topology.queries import (
    DEFAULT_OVERLAY_DEPTH,
    DEFAULT_PATH_MAX_HOPS,
    MAX_OVERLAY_DEPTH,
    MAX_PATH_MAX_HOPS,
)
from meho_backplane.ui.templating import get_templates

__all__ = ["build_table_router"]


class _ViewMode(StrEnum):
    """Closed enum of view modes exposed on ``GET /ui/topology``.

    ``table`` is the default (T1 / #880); ``graph`` switches to the
    Cytoscape.js surface (T2 / #881). The enum's ``str`` mixin keeps
    template URL building stable -- ``str(_ViewMode.GRAPH)`` is
    ``"graph"`` (not ``"_ViewMode.GRAPH"``). An out-of-range value
    fails Pydantic validation (422) at the HTTP boundary so the route
    body never sees an unknown mode.
    """

    TABLE = "table"
    GRAPH = "graph"


class _SortColumn(StrEnum):
    """Closed enum of sort columns exposed in the URL.

    Mirrors :data:`meho_backplane.topology.query._NODE_SORT_COLUMNS`
    -- redeclared at the route boundary so an out-of-range value
    fails Pydantic validation (422 with the candidate list in the
    error context) before the substrate's defensive
    :class:`ValueError` guard runs. The enum's ``str`` mixin keeps
    the template's ``{{ sort }}`` rendering / ``href`` building
    stable -- ``str(SortColumn.NAME)`` is ``"name"`` (not
    ``"_SortColumn.NAME"``).
    """

    NAME = "name"
    KIND = "kind"
    LAST_SEEN = "last_seen"
    FIRST_SEEN = "first_seen"


class _SortDirection(StrEnum):
    """Sort direction enum -- ``asc`` (default) or ``desc``."""

    ASC = "asc"
    DESC = "desc"


#: Page-size cap surfaced at the HTTP boundary. Tighter than the
#: substrate's ``_MAX_NODE_LIMIT`` (1000); the UI table renders the
#: full page in one HTMX swap, so showing more than a couple of
#: hundred rows at once is past the operator's working memory.
_LIMIT_DEFAULT: Final[int] = 50
_LIMIT_MAX: Final[int] = 500


def _is_htmx_request(request: Request) -> bool:
    """Return ``True`` when the request was issued by HTMX.

    HTMX 2 sets ``HX-Request: true`` on every fetch its directives
    drive (see https://htmx.org/reference/#request_headers). The
    handler branches on this header to decide between rendering the
    full page (a normal navigation) and the table-body fragment (a
    sort / filter swap). The check is case-insensitive because the
    HTTP header tabulation is case-insensitive by spec.
    """
    return request.headers.get("hx-request", "").lower() == "true"


def _next_direction(current_sort: _SortColumn, target_sort: str, direction: _SortDirection) -> str:
    """Compute the next sort direction for a column header link.

    A click on the currently-active sort column toggles asc/desc; a
    click on a different column resets to asc. Pure helper so the
    template's ``href`` builder stays a one-liner and the
    asc/desc-toggle behaviour is unit-testable via the substrate
    helpers alone.
    """
    if current_sort.value == target_sort:
        return (
            _SortDirection.DESC.value
            if direction == _SortDirection.ASC
            else _SortDirection.ASC.value
        )
    return _SortDirection.ASC.value


async def _render_table(
    request: Request,
    *,
    sort: _SortColumn = _SortColumn.NAME,
    direction: _SortDirection = _SortDirection.ASC,
    kind: str | None = None,
    name_contains: str | None = None,
    limit: int = _LIMIT_DEFAULT,
    selected_id: uuid.UUID | None = None,
    session_ctx: UISessionContext = Depends(require_ui_session),
    db_session: AsyncSession = Depends(get_raw_session),
) -> HTMLResponse:
    """Render ``GET /ui/topology[?view=table]``.

    Pulls the tenant's nodes via :func:`list_nodes` and renders
    either the full page (browser nav) or the table-body fragment
    (HTMX swap) per the ``HX-Request`` header.

    Filter + sort inputs are echoed back into the template context
    so the rendered HTML preserves the operator's selection (the
    ``<select>`` keeps its value, the column header keeps the
    active-direction arrow, the filter input keeps its text). Both
    the page and the fragment receive the same context shape so the
    template fragments stay interchangeable.

    ``selected_id`` is the cross-link payload from the graph view's
    node tap: when present, the matching row is marked with
    ``data-selected="true"`` so a small inline script can scroll it
    into view + highlight it. Cross-tenant ids decay safely: the row
    simply does not exist in the rendered fragment, so the
    ``data-selected`` marker no-ops.
    """
    nodes = await list_nodes(
        db_session,
        session_ctx.tenant_id,
        kind=kind,
        name_contains=name_contains,
        sort=sort.value,
        direction=direction.value,
        limit=limit,
    )
    is_fragment = _is_htmx_request(request)
    # The page header carries the tenant_admin-only "Bulk import" button
    # (Task #1954). Resolve the role probe only for the full-page render —
    # the table-body fragment swap (sort / filter) does not re-render the
    # header chrome, so the JWT round-trip would be wasted there. Fail-soft:
    # a JWKS hiccup projects to "no privileges" (the button hides) rather
    # than 5xx-ing the table; the server-side ``tenant_admin`` gate on the
    # bulk routes is the authority, not this hint.
    is_tenant_admin = False
    if not is_fragment:
        is_tenant_admin = (await resolve_role_probe(request, session_ctx)).is_tenant_admin
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context = {
        "page_title": "Topology",
        "active_surface": "topology",
        "nodes": nodes,
        # ``is_fragment`` drives the sortable head's out-of-band re-render:
        # on an HTMX sort / filter swap the ``_table_rows.html`` fragment
        # emits ``_table_head.html`` with ``hx-swap-oob`` so the head's
        # ``next_dir`` links + active-column arrow track the new sort state
        # (issue #140). On the full page the head renders in-place via
        # ``table.html`` and ``is_fragment`` is false so it is not
        # double-emitted. ``oob`` is a Jinja ``StrictUndefined``-safe
        # default so ``_table_head.html``'s ``{% if oob %}`` guard never
        # reads an undefined name when the head is included in-place.
        "is_fragment": is_fragment,
        "oob": False,
        "sort": sort,
        "direction": direction,
        "kind_filter": kind or "",
        "name_filter": name_contains or "",
        "csrf_token": csrf_token,
        "is_tenant_admin": is_tenant_admin,
        "bulk_import_href": "/ui/topology/edges/bulk",
        "next_direction_for": _next_direction_factory(sort, direction),
        "node_kind_options": _node_kind_options(),
        # ``selected_id`` is the cross-link payload from the graph
        # surface ("show in table" / a Cytoscape node tap). The empty
        # string represents "no selection" so Jinja's ``StrictUndefined``
        # env does not raise on a missing-key read in the template.
        "selected_id": str(selected_id) if selected_id is not None else "",
    }
    template_name = "topology/_table_rows.html" if is_fragment else "topology/table.html"
    response = get_templates().TemplateResponse(request, template_name, context)
    # Mirror the dashboard's CSRF posture so a future state-changing
    # action button on the table (bulk-delete, bulk-annotate, ...) has
    # the double-submit chain in place from request one. The cookie
    # is intentionally not ``HttpOnly`` -- HTMX needs to read it to
    # populate ``X-CSRF-Token`` on the outbound request.
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )
    return response


def _next_direction_factory(
    current_sort: _SortColumn,
    current_direction: _SortDirection,
) -> object:
    """Build a closure the template can call to compute the next direction.

    Returned as an opaque ``object`` (typed loosely so Jinja's
    ``StrictUndefined`` doesn't complain when the template calls
    ``next_direction_for('kind')``); the implementation is a small
    one-arg callable that wraps :func:`_next_direction` with the
    current state pre-bound. Keeping it as a closure (rather than a
    module-level function the template imports) means the template
    doesn't need to learn about ``_SortColumn`` / ``_SortDirection``
    -- it just calls ``next_direction_for(col_name)``.
    """

    def _call(target: str) -> str:
        return _next_direction(current_sort, target, current_direction)

    return _call


def _node_kind_options() -> list[str]:
    """Return the closed-enum list of kinds for the filter dropdown.

    Sourced from :data:`meho_backplane.db.models._GRAPH_NODE_KINDS`,
    the same closed vocabulary the DB-layer CHECK constraint pins.
    Deriving the dropdown from the *current page's* rows was wrong
    for two reasons:

    * the table is paged (default 50, hard cap 500) so kinds beyond
      page 1 silently vanish from the filter UI, and
    * once a filter is applied the dropdown collapses to that one
      kind, blocking the operator from clearing back to a different
      kind without manually editing the URL.

    Using the closed enum gives the operator the full kind vocabulary
    regardless of paging or active filter, at zero substrate cost
    (no extra ``SELECT DISTINCT`` round trip). Sorted alphabetically
    for stable rendering across page loads.
    """
    return sorted(_GRAPH_NODE_KINDS)


#: Module-level :class:`fastapi.Depends` closures -- required to satisfy
#: ruff B008 (a function call in a default argument position is
#: disallowed in this codebase except for the FastAPI-blessed call sites
#: enumerated in ``flake8-bugbear.extend-immutable-calls``).
_require_ui_session_dep = Depends(require_ui_session)
_get_raw_session_dep = Depends(get_raw_session)


def _resolve_overlay_direction(
    *,
    direction: str,
    from_: str | None,
    to: str | None,
) -> OverlayDirection | None:
    """Decide whether ``direction`` carries an overlay sense on the graph branch.

    Returns the parsed :class:`OverlayDirection` when the dependents/
    dependencies overlay is active (``from`` set, ``to`` unset) and
    ``direction`` parses as one of the enum members. Returns ``None``
    otherwise -- the route's default (dependents) applies. An
    out-of-range value on the graph branch is a silent default (rather
    than 422) so the same ``direction=asc`` value can ride through the
    URL when an operator toggles between table and graph without
    breaking the "preserve filters" contract documented in #881.
    """
    if from_ is None or to is not None:
        return None
    try:
        return OverlayDirection(direction)
    except ValueError:
        return None


def _resolve_overlay_depth(
    *,
    depth: int | None,
    from_: str | None,
    to: str | None,
) -> int | None:
    """Pick the effective ``depth`` for the active branch.

    Active overlay (``from`` set, ``to`` unset) -> ``depth`` if
    supplied, else :data:`DEFAULT_OVERLAY_DEPTH`. Inactive branches
    (full inventory, path overlay) -> ``None`` (depth does not
    apply).
    """
    if depth is not None:
        return depth
    if from_ is not None and to is None:
        return DEFAULT_OVERLAY_DEPTH
    return None


def _resolve_path_max_hops(
    *,
    max_hops: int | None,
    from_: str | None,
    to: str | None,
) -> int | None:
    """Pick the effective ``max_hops`` for the path overlay.

    Path overlay (both ``from`` + ``to`` set) -> ``max_hops`` if
    supplied, else :data:`DEFAULT_PATH_MAX_HOPS`. Other branches ->
    ``None``.
    """
    if max_hops is not None:
        return max_hops
    if from_ is not None and to is not None:
        return DEFAULT_PATH_MAX_HOPS
    return None


def _validate_sort_direction(direction: str) -> _SortDirection:
    """Parse ``direction`` against the table-branch sort enum.

    Out-of-range -> :class:`HTTPException` 422 with a structured
    diagnostic. Same posture as the pre-T3 Pydantic enum validator.
    """
    try:
        return _SortDirection(direction)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                f"direction must be one of {[d.value for d in _SortDirection]}; got {direction!r}"
            ),
        ) from exc


def build_table_router() -> APIRouter:
    """Construct the topology-table :class:`APIRouter`.

    Registers the single ``GET /ui/topology`` route that serves the
    full page, the HTMX table fragment, and the Cytoscape graph
    surface from one handler -- branching on ``?view=``. The route
    name (``ui_topology_table``) is referenced by ``url_for`` in the
    chassis sidebar template -- a future rename here must update the
    sidebar in lockstep.
    """
    router = APIRouter(tags=["ui-topology"])

    async def _handler(
        request: Request,
        sort: _SortColumn = Query(default=_SortColumn.NAME),
        # ``direction`` is dual-purpose: the table branch consumes
        # ``asc``/``desc`` (sort order, validated again by
        # ``_validate_sort_direction``); the graph overlay branch
        # consumes ``dependents``/``dependencies`` (traversal
        # direction, validated by ``_resolve_overlay_direction``).
        # The OpenAPI ``pattern`` constrains the union of accepted
        # values so the generated CLI client + downstream contract
        # consumers see the real vocabulary rather than a free-form
        # ``string``. Out-of-union values 422 at the HTTP boundary --
        # ``asc``/``desc`` still ride through cleanly when the
        # operator toggles between table and graph views (the
        # "preserve filters" contract documented in #881).
        direction: str = Query(
            default=_SortDirection.ASC.value,
            max_length=32,
            pattern=r"^(asc|desc|dependents|dependencies)$",
            description=(
                "Dual-purpose: ``asc`` / ``desc`` on the table branch "
                "(``view=table``), ``dependents`` / ``dependencies`` on "
                "the graph overlay branch (``view=graph&from=<name>``)."
            ),
        ),
        # ``kind`` / ``q`` coerce ``"" -> None`` so the filter bar's
        # "All kinds" option (``<option value="">``) and a cleared
        # search box mean "no filter". The form co-submits ``kind`` on
        # every search keystroke (``hx-include="closest form"``), so
        # without the coercion an empty ``kind`` reaches
        # :func:`list_nodes` as an exact match on ``''`` and wipes the
        # grid. ``max_length`` rides on the inner ``str`` branch via
        # ``StringConstraints`` -- a bare ``Query(max_length=...)`` on
        # the nullable field would apply the guard to the ``None`` the
        # BeforeValidator produces and raise ``TypeError``.
        kind: Annotated[
            Annotated[str, StringConstraints(max_length=64)] | None,
            EMPTY_STR_TO_NONE,
            Query(),
        ] = None,
        q: Annotated[
            Annotated[str, StringConstraints(max_length=256)] | None,
            EMPTY_STR_TO_NONE,
            Query(),
        ] = None,
        limit: int = Query(default=_LIMIT_DEFAULT, ge=1, le=_LIMIT_MAX),
        view: _ViewMode = Query(default=_ViewMode.TABLE),
        selected: uuid.UUID | None = Query(default=None),
        # G10.5-T3 (#882) overlay query params. ``from`` / ``to`` are
        # reserved Python keywords so they ride as ``alias=`` on the
        # FastAPI Query so the URL contract reads ``?from=&to=`` and
        # the Python signature stays clean (a bare ``from`` would be
        # a Python parse error).
        from_: str | None = Query(default=None, alias="from", max_length=256),
        from_kind: str | None = Query(default=None, max_length=64),
        to: str | None = Query(default=None, max_length=256),
        to_kind: str | None = Query(default=None, max_length=64),
        depth: int | None = Query(default=None, ge=1, le=MAX_OVERLAY_DEPTH),
        max_hops: int | None = Query(default=None, ge=1, le=MAX_PATH_MAX_HOPS),
        session_ctx: UISessionContext = _require_ui_session_dep,
        db_session: AsyncSession = _get_raw_session_dep,
    ) -> HTMLResponse:
        """Serve the topology UI, branching on ``?view=``. See module
        docstring for the URL contract + dual-purpose ``direction``
        semantics (table sort vs graph overlay).
        """
        if view == _ViewMode.GRAPH:
            return await render_graph(
                request,
                session_ctx=session_ctx,
                db_session=db_session,
                kind=kind,
                name_contains=q,
                selected_id=selected,
                from_name=from_,
                from_kind=from_kind,
                to_name=to,
                to_kind=to_kind,
                direction=_resolve_overlay_direction(direction=direction, from_=from_, to=to),
                depth=_resolve_overlay_depth(depth=depth, from_=from_, to=to),
                max_hops=_resolve_path_max_hops(max_hops=max_hops, from_=from_, to=to),
            )
        return await _render_table(
            request,
            sort=sort,
            direction=_validate_sort_direction(direction),
            kind=kind,
            name_contains=q,
            limit=limit,
            selected_id=selected,
            session_ctx=session_ctx,
            db_session=db_session,
        )

    router.add_api_route(
        "/ui/topology",
        _handler,
        methods=["GET"],
        name="ui_topology_table",
        response_class=HTMLResponse,
        # The ``?view=graph`` overlay branches (dependents / dependencies
        # / path) call :func:`render_graph`, which catches
        # :class:`NodeNotFoundError` / :class:`AmbiguousNodeError` and
        # returns an ``HTMLResponse(status_code=404|409)`` carrying the
        # overlay-error fragment. Surfacing those statuses in the
        # OpenAPI snapshot keeps the generated CLI client + downstream
        # contract consumers in sync with the runtime behaviour.
        responses={
            404: {
                "description": (
                    "Overlay anchor (``?from=<name>``) or path endpoint "
                    "(``?from=&to=``) does not resolve in the caller's "
                    "tenant. Returns the overlay-error fragment."
                ),
                "content": {"text/html": {}},
            },
            409: {
                "description": (
                    "Overlay anchor or path endpoint is a bare name that "
                    "resolves to multiple kinds in the caller's tenant "
                    "(``kind=`` disambiguation required). Returns the "
                    "overlay-error fragment with the candidate kinds."
                ),
                "content": {"text/html": {}},
            },
        },
    )
    return router
