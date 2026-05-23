# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /ui/topology`` -- the tenant topology tabular surface.

Initiative #342 (G10.5 Topology UI), Task #880 (G10.5-T1) work item
#1. Renders the per-tenant node inventory as a server-side sortable +
HTMX-filterable table with multi-row checkbox select.

Two response shapes share one handler:

* **Full page** -- a normal browser navigation to ``/ui/topology``
  (no ``HX-Request`` header) returns the full ``topology/table.html``
  page (extends ``base.html``; renders the navbar, sidebar, filter
  bar, sort headers, table body, and the empty ``#node-drawer``
  slot the per-node drawer swaps into).

* **Fragment** -- an HTMX-driven sort / filter
  (``hx-get="/ui/topology" hx-target="#topology-table-body"`` plus
  ``hx-trigger="input changed delay:300ms, keyup changed delay:300ms"``
  on the filter inputs) carries ``HX-Request: true`` and the handler
  returns the ``topology/_table_rows.html`` partial -- just the
  ``<tbody>`` content swapped into the existing table without a
  full-page reload.

Tenant scoping is non-overrideable. Every call to
:func:`meho_backplane.topology.query.list_nodes` passes
``operator.tenant_id`` from the session-bound
:class:`UISessionContext`; no query parameter or body field carries
a tenant id. Another tenant's node never renders -- the acceptance
criterion is enforced at the substrate layer (the listing SQL's
first ``WHERE`` clause), not the template.

Sort column + direction defaults are the ``name`` column ascending
-- a stable, human-meaningful order. ``view=table`` in the query
string is currently a no-op (T2 (#881) will introduce ``view=graph``
and switch routing accordingly); the route accepts but does not
require it so a future graph view can land without an external URL
contract change.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Final

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_raw_session
from meho_backplane.topology.query import TopologyNodeListEntry, list_nodes
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.templating import get_templates

__all__ = ["build_table_router"]


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
    session_ctx: UISessionContext = Depends(require_ui_session),
    db_session: AsyncSession = Depends(get_raw_session),
) -> HTMLResponse:
    """Render ``GET /ui/topology``.

    Pulls the tenant's nodes via :func:`list_nodes` and renders
    either the full page (browser nav) or the table-body fragment
    (HTMX swap) per the ``HX-Request`` header.

    Filter + sort inputs are echoed back into the template context
    so the rendered HTML preserves the operator's selection (the
    ``<select>`` keeps its value, the column header keeps the
    active-direction arrow, the filter input keeps its text). Both
    the page and the fragment receive the same context shape so the
    template fragments stay interchangeable.
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
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context = {
        "page_title": "Topology",
        "nodes": nodes,
        "sort": sort,
        "direction": direction,
        "kind_filter": kind or "",
        "name_filter": name_contains or "",
        "csrf_token": csrf_token,
        "next_direction_for": _next_direction_factory(sort, direction),
        "node_kind_options": _node_kind_options(nodes),
        # The footer in ``base.html`` reads ``ready`` to colour the
        # readiness pill; topology does not poll readiness itself
        # (the dashboard owns that surface), so ship ``False`` so
        # the ``StrictUndefined`` env does not raise on the read.
        "ready": False,
    }
    template_name = (
        "topology/_table_rows.html" if _is_htmx_request(request) else "topology/table.html"
    )
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


def _node_kind_options(nodes: Iterable[TopologyNodeListEntry]) -> list[str]:
    """Derive the kind-filter dropdown options from the current rows.

    The dropdown shows only kinds the operator's tenant actually has
    -- a v0.2-onboarding tenant with one connector should not see
    eight irrelevant filter options. Sorted alphabetically for stable
    rendering across page loads.
    """
    return sorted({node.kind for node in nodes})


#: Module-level :class:`fastapi.Depends` closures -- required to satisfy
#: ruff B008 (a function call in a default argument position is
#: disallowed in this codebase except for the FastAPI-blessed call sites
#: enumerated in ``flake8-bugbear.extend-immutable-calls``).
_require_ui_session_dep = Depends(require_ui_session)
_get_raw_session_dep = Depends(get_raw_session)


def build_table_router() -> APIRouter:
    """Construct the topology-table :class:`APIRouter`.

    Registers the single ``GET /ui/topology`` route that serves both
    the full page and the HTMX fragment from one handler. The route
    name (``ui_topology_table``) is referenced by ``url_for`` in the
    chassis sidebar template -- a future rename here must update the
    sidebar in lockstep.
    """
    router = APIRouter(tags=["ui-topology"])

    async def _handler(
        request: Request,
        sort: _SortColumn = Query(default=_SortColumn.NAME),
        direction: _SortDirection = Query(default=_SortDirection.ASC),
        kind: str | None = Query(default=None, max_length=64),
        q: str | None = Query(default=None, max_length=256),
        limit: int = Query(default=_LIMIT_DEFAULT, ge=1, le=_LIMIT_MAX),
        view: str | None = Query(default=None, max_length=16),
        session_ctx: UISessionContext = _require_ui_session_dep,
        db_session: AsyncSession = _get_raw_session_dep,
    ) -> HTMLResponse:
        """``GET /ui/topology[?view=table&sort=...&direction=...&kind=...&q=...]``.

        ``view`` is accepted but unused in T1 (the only mode is the
        table); T2 (#881) will branch on ``view`` to render the
        Cytoscape graph instead. Documenting the param in the
        signature now keeps the URL contract forward-compatible.
        """
        return await _render_table(
            request,
            sort=sort,
            direction=direction,
            kind=kind,
            name_contains=q,
            limit=limit,
            session_ctx=session_ctx,
            db_session=db_session,
        )

    router.add_api_route(
        "/ui/topology",
        _handler,
        methods=["GET"],
        name="ui_topology_table",
        response_class=HTMLResponse,
    )
    return router
