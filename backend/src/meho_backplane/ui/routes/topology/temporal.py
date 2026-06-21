# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Temporal **read** views for the topology console (in-process BFF).

Initiative #1941 (G10.17 Topology console), Task #1955 (T3). The read-only
topology surface ships the inventory table, the Cytoscape graph, the node
drawer, and the dependents / dependencies closure overlays (#880 / #881 /
#882) — but the *temporal* trio (per-resource history, the tenant-wide
change feed, and a point-in-time diff) has no non-graph presentation. Those
three verbs exist only on the CLI (``meho topology history`` / ``timeline``
/ ``diff``) and the Bearer REST API (``GET /api/v1/topology/{timeline,
history/{name},diff}``). This module is the operator-console face of them.

Why a session BFF and not the Bearer ``/api/v1/topology/*`` routes
-------------------------------------------------------------------

The REST temporal routes are Bearer-gated over a verified JWT. A browser
carrying only the BFF session cookie cannot authenticate them. So this
module adds ``/ui/topology/{timeline,history/{name},diff}`` sub-routes that
are ``require_ui_session`` and call the
:mod:`~meho_backplane.topology.query` **service** in-process
(:func:`~meho_backplane.topology.query.query_timeline` /
:func:`~meho_backplane.topology.query.query_history` /
:func:`~meho_backplane.topology.query.query_diff`) — the same console-surface
pattern the edge-write sibling (Task #1953) and the approvals / connectors
surfaces use. All three are **GET** reads, so they carry no write/CSRF
surface; the in-process call keeps the synchronous-audit binding.

RBAC + the audit footgun (load-bearing)
---------------------------------------

All three reads are **``operator``** at the REST layer; the BFF mirrors that
(any authenticated session reconstructs an operator via
:func:`~meho_backplane.ui.routes.connectors.operator.lift_operator_from_session`).

The three REST routes bind ``audit_op_class="audit_query"`` (NOT ``read``)
so the broadcast event carries **row-count only, never the per-row payload**
(``api/v1/topology.py`` ``timeline`` / ``history`` / ``diff``). A history
snapshot or a timeline row can name a sensitive resource; downgrading the
audit class to ``read`` would leak that payload onto the SSE / Slack feed.
This module binds the **same** ``audit_op_class="audit_query"`` via
:func:`structlog.contextvars.bind_contextvars` (the UI-BFF precedent for
binding the audit class is
:mod:`meho_backplane.ui.routes.conventions.write`) — so the console reads
never downgrade the class. The op-ids mirror the REST constants verbatim so
the audit / dashboard classes line up across the CLI / REST / UI fronts.

Recoverable typed states
------------------------

The service raises HTTP-agnostic ``ValueError`` subclasses; this module maps
each to a re-rendered panel with a legible banner rather than a dead 5xx,
mirroring the REST layer's status codes:

* :class:`~meho_backplane.topology.resolvers.AmbiguousNodeError` (history) →
  the **409 ``ambiguous_node``** banner listing the candidate ``kinds`` so
  the operator re-submits with ``?kind=`` (the REST 409 surface).
* :class:`~meho_backplane.topology.resolvers.NodeNotFoundError` (history) →
  an empty / 404 panel state, not a 500.
* :class:`~meho_backplane.targets.resolver.TargetNotFoundError` (timeline's
  optional ``target`` filter) → a recoverable banner with the near-miss
  suggestions, not the raw 404 envelope.
* :class:`~meho_backplane.topology.timeline_cursor.InvalidTimelineCursorError`
  (timeline) → a 400 banner ("the page cursor is invalid; reload the feed").
* The diff's ``truncated`` / ``truncation_hint`` (overflow of the 1000-row
  ``_DIFF_HARD_CAP``) surfaces as a "narrow the time window" banner rather
  than a silently-clipped list.

Tenant isolation
----------------

Every read derives ``tenant_id`` from the reconstructed
:class:`~meho_backplane.auth.operator.Operator` (itself validated against the
session) — never a query field. The service scopes on ``operator.tenant_id``,
so a cross-tenant target / node name is indistinguishable from a missing one.
"""

from __future__ import annotations

from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_raw_session
from meho_backplane.targets.resolver import TargetNotFoundError, resolve_target
from meho_backplane.topology.query import (
    query_diff,
    query_history,
    query_timeline,
)
from meho_backplane.topology.resolvers import AmbiguousNodeError, NodeNotFoundError
from meho_backplane.topology.timeline_cursor import InvalidTimelineCursorError
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.routes.connectors.operator import lift_operator_from_session
from meho_backplane.ui.templating import get_templates

__all__ = ["build_temporal_router"]

_log = structlog.get_logger(__name__)

#: Canonical audit op-ids, mirrored verbatim from the REST surface
#: (``api/v1/topology.py`` ``_OP_TIMELINE`` / ``_OP_HISTORY`` / ``_OP_DIFF``)
#: so the in-process BFF reads land in the same G8 audit / dashboard classes
#: as the Bearer routes. Pinned as constants so the contract is greppable and
#: a typo surfaces at call time rather than broadcasting under the wrong id.
_OP_TIMELINE = "topology.timeline"
_OP_HISTORY = "topology.history"
_OP_DIFF = "topology.diff"

#: The audit class every temporal read binds. NOT ``read`` — see the module
#: docstring's "audit footgun": ``audit_query`` keeps the broadcast event to
#: row-count only so a per-row history / timeline payload never leaks.
_AUDIT_QUERY_CLASS = "audit_query"

#: Page size for the timeline feed. Mirrors the REST default
#: (``_TIMELINE_LIMIT_DEFAULT``); the keyset cursor carries the rest of the
#: chronology behind an HTMX "load more" swap.
_TIMELINE_PAGE_SIZE = 50

#: Module-level ``Depends`` closures (ruff B008 guard), matching the
#: convention every UI surface router follows.
_require_session = Depends(require_ui_session)
_get_raw_session_dep = Depends(get_raw_session)


def _bind_audit_query(op_id: str) -> None:
    """Bind the ``audit_op_id`` / ``audit_op_class`` contextvars for a read.

    Mirrors the REST temporal routes' binding
    (``api/v1/topology.py`` timeline / history / diff): the chassis audit
    middleware lifts these into the audit_log row (stripped ``audit_``
    prefix), and the broadcast publisher re-reads ``op_class`` to keep the
    event aggregate-only. Binding ``audit_query`` (never ``read``) is the
    load-bearing line — it stops the per-row payload from leaking onto the
    SSE / Slack feed.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=op_id,
        audit_op_class=_AUDIT_QUERY_CLASS,
    )


def _bind_row_count(count: int) -> None:
    """Bind the aggregate row count the broadcast event carries.

    The only request-derived signal the ``audit_query`` broadcast exposes —
    never the rows themselves. Mirrors the REST routes' trailing
    ``bind_contextvars(audit_row_count=len(result.rows))``.
    """
    structlog.contextvars.bind_contextvars(audit_row_count=count)


async def _render_timeline(
    request: Request,
    *,
    operator: Operator,
    db_session: AsyncSession,
    target: str | None,
    since: datetime | None,
    until: datetime | None,
    cursor: str | None,
) -> HTMLResponse:
    """Resolve the optional target filter + render the change-feed page.

    On a browser nav (no ``HX-Request``) renders the full
    ``topology/timeline.html`` page; on an HTMX "load more" request renders
    the ``topology/_timeline_rows.html`` fragment so the next keyset page
    appends (``hx-swap="beforeend"``) without re-rendering the chrome. A
    ``target`` that does not resolve renders a recoverable near-miss banner;
    a tampered ``cursor`` renders a 400 banner — neither is a dead 5xx.
    """
    target_id = None
    if target is not None and target.strip():
        try:
            resolved = await resolve_target(db_session, operator.tenant_id, target.strip())
        except TargetNotFoundError as exc:
            return _render_timeline_target_error(request, target=target.strip(), exc=exc)
        target_id = resolved.id

    try:
        result = await query_timeline(
            operator,
            target_id=target_id,
            since=since,
            until=until,
            limit=_TIMELINE_PAGE_SIZE,
            cursor=cursor,
        )
    except InvalidTimelineCursorError as exc:
        return _render_timeline_cursor_error(request, message=str(exc))

    _bind_row_count(len(result.rows))
    _log.info(
        "ui_topology_timeline_read",
        rows=len(result.rows),
        has_more=result.next_cursor is not None,
        operator_sub=operator.sub,
        tenant_id=str(operator.tenant_id),
    )

    context: dict[str, object] = {
        "page_title": "Topology · Timeline",
        "active_surface": "topology",
        "rows": result.rows,
        "next_cursor": result.next_cursor or "",
        "target_filter": target.strip() if target else "",
        "load_more_href": _timeline_load_more_href(
            target=target,
            since=since,
            until=until,
            cursor=result.next_cursor,
        ),
    }
    template_name = (
        "topology/_timeline_rows.html" if _is_htmx_request(request) else "topology/timeline.html"
    )
    return get_templates().TemplateResponse(request, template_name, context)


async def _render_history(
    request: Request,
    name: str,
    *,
    operator: Operator,
    kind: str | None,
) -> HTMLResponse:
    """Render the per-resource history panel for *name*.

    The full chronology arrives in one response (history has no cursor). A
    bare ``name`` resolving to more than one kind renders the **409
    ``ambiguous_node``** banner listing the candidate ``kinds`` (re-submit
    with ``?kind=``); an unknown name renders an empty / 404 panel — not a
    500.
    """
    try:
        result = await query_history(operator, name, kind=kind)
    except AmbiguousNodeError as exc:
        return _render_history_ambiguous(request, name=name, exc=exc)
    except NodeNotFoundError as exc:
        return _render_history_not_found(request, name=name, exc=exc)

    _bind_row_count(len(result.rows))
    _log.info(
        "ui_topology_history_read",
        name=name,
        kind=kind,
        rows=len(result.rows),
        operator_sub=operator.sub,
        tenant_id=str(operator.tenant_id),
    )

    context: dict[str, object] = {
        "page_title": "Topology · History",
        "active_surface": "topology",
        "node_name": name,
        "node_kind": kind or "",
        "anchor_node_id": str(result.anchor_node_id),
        "rows": result.rows,
    }
    return get_templates().TemplateResponse(request, "topology/history.html", context)


async def _render_diff(
    request: Request,
    *,
    operator: Operator,
    ts1: datetime,
    ts2: datetime,
    kind: str | None,
) -> HTMLResponse:
    """Render the point-in-time diff panel between *ts1* (excl) and *ts2* (incl).

    Surfaces the net created / updated / removed entries. On overflow of the
    1000-row ``_DIFF_HARD_CAP`` the service flags ``truncated`` + a
    ``truncation_hint``; this panel renders the hint as a "narrow the time
    window" banner rather than presenting a silently-clipped list as if it
    were complete.
    """
    result = await query_diff(operator, ts1=ts1, ts2=ts2, kind_filter=kind)

    _bind_row_count(len(result.entries))
    _log.info(
        "ui_topology_diff_read",
        rows=len(result.entries),
        truncated=result.truncated,
        operator_sub=operator.sub,
        tenant_id=str(operator.tenant_id),
    )

    context: dict[str, object] = {
        "page_title": "Topology · Diff",
        "active_surface": "topology",
        "ts1": ts1.isoformat(),
        "ts2": ts2.isoformat(),
        "kind_filter": kind or "",
        "entries": result.entries,
        "truncated": result.truncated,
        "truncation_hint": result.truncation_hint or "",
    }
    return get_templates().TemplateResponse(request, "topology/diff.html", context)


def _render_history_ambiguous(
    request: Request,
    *,
    name: str,
    exc: AmbiguousNodeError,
) -> HTMLResponse:
    """Render the 409 ``ambiguous_node`` recoverable banner for history.

    Lists the candidate ``kinds`` so the operator re-submits with ``?kind=``.
    Mirrors the REST 409 surface (``_ambiguous_node_http``) and the
    annotate-modal's ambiguous-node re-render.
    """
    context: dict[str, object] = {
        "page_title": "Topology · History",
        "active_surface": "topology",
        "node_name": name,
        "ambiguous_kinds": sorted(exc.kinds),
    }
    return get_templates().TemplateResponse(
        request,
        "topology/history_ambiguous.html",
        context,
        status_code=status.HTTP_409_CONFLICT,
    )


def _render_history_not_found(
    request: Request,
    *,
    name: str,
    exc: NodeNotFoundError,
) -> HTMLResponse:
    """Render the empty / 404 history panel for an unknown name.

    A missing node is an expected operator typo, not a server fault — the
    panel states "no node matched" rather than a dead 500. Mirrors the REST
    404 ``node_not_found`` surface.
    """
    context: dict[str, object] = {
        "page_title": "Topology · History",
        "active_surface": "topology",
        "node_name": name,
        "node_kind": exc.kind or "",
    }
    return get_templates().TemplateResponse(
        request,
        "topology/history_not_found.html",
        context,
        status_code=status.HTTP_404_NOT_FOUND,
    )


def _render_timeline_target_error(
    request: Request,
    *,
    target: str,
    exc: TargetNotFoundError,
) -> HTMLResponse:
    """Render the recoverable near-miss banner for an unresolved target filter.

    The ``target`` query filter did not resolve to a row in the tenant. Pulls
    the near-miss ``matches`` from the resolver's 404 envelope so the operator
    can correct the name, rather than dead-ending on the raw 404.
    """
    detail: dict[str, object] = exc.detail if isinstance(exc.detail, dict) else {}
    matches = detail.get("matches", [])
    match_names = (
        [m.get("name", "") for m in matches if isinstance(m, dict)]
        if isinstance(matches, list)
        else []
    )
    context: dict[str, object] = {
        "page_title": "Topology · Timeline",
        "active_surface": "topology",
        "target_filter": target,
        "near_misses": match_names,
    }
    return get_templates().TemplateResponse(
        request,
        "topology/timeline_target_not_found.html",
        context,
        status_code=status.HTTP_404_NOT_FOUND,
    )


def _render_timeline_cursor_error(request: Request, *, message: str) -> HTMLResponse:
    """Render the 400 banner for a tampered / invalid page cursor.

    The opaque keyset cursor is a forward-only token from a prior response; a
    hand-crafted or stale one renders a "reload the feed" banner rather than a
    500. Mirrors the REST 400 ``invalid_cursor`` surface.
    """
    context: dict[str, object] = {
        "page_title": "Topology · Timeline",
        "active_surface": "topology",
        "cursor_error": message,
    }
    return get_templates().TemplateResponse(
        request,
        "topology/timeline_cursor_error.html",
        context,
        status_code=status.HTTP_400_BAD_REQUEST,
    )


def _timeline_load_more_href(
    *,
    target: str | None,
    since: datetime | None,
    until: datetime | None,
    cursor: str | None,
) -> str:
    """Build the ``hx-get`` URL the "load more" control fires for the next page.

    Carries the keyset ``cursor`` plus the active filters so the next page is
    drawn from the same window. Returns ``""`` when there is no next page so
    the template omits the control. :func:`urllib.parse.urlencode` percent-
    encodes the cursor (base64 may contain ``+`` / ``=``) and any target name.
    """
    if cursor is None:
        return ""
    from urllib.parse import urlencode

    params: dict[str, str] = {"cursor": cursor}
    if target is not None and target.strip():
        params["target"] = target.strip()
    if since is not None:
        params["since"] = since.isoformat()
    if until is not None:
        params["until"] = until.isoformat()
    return f"/ui/topology/timeline?{urlencode(params)}"


def _is_htmx_request(request: Request) -> bool:
    """True when the request carries the ``HX-Request`` header HTMX sets.

    Distinguishes a browser nav (full ``base.html`` page) from an HTMX "load
    more" swap (the rows fragment only). Mirrors ``table.py``'s
    ``_is_htmx_request``.
    """
    return request.headers.get("HX-Request", "").lower() == "true"


def build_temporal_router() -> APIRouter:
    """Construct the ``/ui/topology/{timeline,history,diff}`` read router.

    Factory function (not a module-level constant) so a test app can
    construct parallel routers without shared route state — the convention
    every topology / approvals / connectors surface router follows. The
    literal ``timeline`` / ``diff`` / ``history`` prefixes are registered
    through this router, which
    :func:`meho_backplane.ui.routes.topology.build_router` includes
    **before** ``build_detail_router()`` so the
    ``/ui/topology/node/{node_id}`` param route never shadows the
    ``/ui/topology/history/{name}`` literal-prefixed route.
    """
    router = APIRouter(tags=["ui-topology"])

    @router.get("/ui/topology/timeline", response_class=HTMLResponse)
    async def timeline(
        request: Request,
        session_ctx: UISessionContext = _require_session,
        db_session: AsyncSession = _get_raw_session_dep,
        target: str | None = Query(default=None, max_length=256),
        since: datetime | None = Query(default=None),
        until: datetime | None = Query(default=None),
        cursor: str | None = Query(default=None, max_length=1024),
    ) -> HTMLResponse:
        """``GET /ui/topology/timeline`` — the tenant-wide change feed."""
        _bind_audit_query(_OP_TIMELINE)
        operator = await lift_operator_from_session(session_ctx)
        return await _render_timeline(
            request,
            operator=operator,
            db_session=db_session,
            target=target,
            since=since,
            until=until,
            cursor=cursor,
        )

    @router.get("/ui/topology/diff", response_class=HTMLResponse)
    async def diff(
        request: Request,
        session_ctx: UISessionContext = _require_session,
        ts1: datetime = Query(..., description="exclusive lower bound on valid_from"),
        ts2: datetime = Query(..., description="inclusive upper bound on valid_from"),
        kind: str | None = Query(default=None, max_length=64),
    ) -> HTMLResponse:
        """``GET /ui/topology/diff?ts1=&ts2=`` — the point-in-time diff."""
        _bind_audit_query(_OP_DIFF)
        operator = await lift_operator_from_session(session_ctx)
        return await _render_diff(
            request,
            operator=operator,
            ts1=ts1,
            ts2=ts2,
            kind=kind,
        )

    @router.get("/ui/topology/history/{name}", response_class=HTMLResponse)
    async def history(
        request: Request,
        name: str,
        session_ctx: UISessionContext = _require_session,
        kind: str | None = Query(default=None, max_length=64),
    ) -> HTMLResponse:
        """``GET /ui/topology/history/{name}`` — per-resource history."""
        _bind_audit_query(_OP_HISTORY)
        operator = await lift_operator_from_session(session_ctx)
        return await _render_history(
            request,
            name,
            operator=operator,
            kind=kind,
        )

    return router
