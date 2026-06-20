# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Batch graph-mutation + refresh routes for the topology console (BFF).

Initiative #1941 (G10.17 Topology console writes), Task #1954 (T2). T1
(#1953, :mod:`~meho_backplane.ui.routes.topology.edges`) shipped the
single-edge annotate / unannotate write surface; this module adds the two
**batch** verbs that exist on the CLI (``meho topology bulk-import`` /
``meho topology refresh``) and the Bearer REST API but had no console face:

* **Bulk import** (``tenant_admin``) — paste / upload an ``edges:`` doc,
  preview it (``dry_run``), then apply it all-or-nothing. The same
  console-surface pattern :mod:`~meho_backplane.ui.routes.connectors.import_router`
  uses for ``targets.yaml`` (paste-or-upload, dry-run preview, in-process
  apply), pointed at
  :func:`~meho_backplane.topology.bulk_import.bulk_import_edges`.
* **Refresh-from-target** (``operator``) — rediscover one target's topology
  and reconcile it into the graph, surfacing the inline
  :class:`~meho_backplane.topology.refresh.RefreshResult` counts. The
  service is **synchronous** (it blocks on the connector probe and returns
  inline — there is no async job surface), so the submit is modelled as a
  long-running HTMX request with ``hx-disabled-elt`` + a spinner, not a
  fake job-status poller.

Why a session BFF and not the Bearer ``/api/v1/topology/*`` routes
-----------------------------------------------------------------

The REST routes are Bearer-gated over a verified JWT. A browser carrying
only the BFF session cookie + the CSRF double-submit token cannot
authenticate them. So this module adds ``/ui/topology/edges/bulk`` +
``/ui/topology/refresh/{target_name}`` sub-routes that are
``require_ui_session`` + CSRF-gated and call the
:mod:`~meho_backplane.topology.bulk_import` /
:mod:`~meho_backplane.topology.refresh` **services** in-process — reusing
the same service layer (and its synchronous-audit binding) the REST
handlers wrap. No new ``/api/v1`` route, no self-HTTP hop the cookie could
not auth anyway.

RBAC tier split (load-bearing)
------------------------------

Bulk import is **``tenant_admin``** (a batch annotate can shrink the
auto-flagged blast radius of ops the operator then runs — the same gate as
``POST /api/v1/targets`` and T1's single-edge writes). Refresh is
**``operator``** (a rediscovery is non-destructive correction of the
auto-discovered landscape). The BFF mirrors this two ways:

* **Soft-hide** — the table-page "Bulk import" button renders only when
  ``is_tenant_admin`` (projected via
  :func:`~meho_backplane.ui.routes.connectors.operator.resolve_role_probe`,
  fail-soft); the drawer's per-target "Refresh" button renders for any
  operator. An operator never sees the bulk affordance.
* **Hard-403** — the bulk routes gate on
  :func:`~meho_backplane.ui.routes.topology.edges._require_topology_admin`
  (the promoted T1 gate), so a forged bulk POST from a non-admin session is
  rejected server-side; the refresh route gates only on
  :func:`~meho_backplane.ui.routes.topology.batch._require_topology_operator`
  so a plain operator succeeds. The same template serving both roles is the
  regression trap — the absent button is UX only; the gate is the authority.

Route ordering
--------------

The literal ``/ui/topology/edges/bulk`` and the ``{target_name}`` route
``/ui/topology/refresh/{target_name}`` must register **ahead of**
``detail.py``'s ``/ui/topology/node/{node_id}`` param route in the
:func:`~meho_backplane.ui.routes.topology.build_router` aggregator (first-
match-wins). ``refresh/{target_name}`` is itself a ``{param}`` route, so it
would shadow — and be shadowed by — a bare ``{param}`` route; registering it
in the aggregator before ``build_detail_router()`` keeps the discipline
explicit (a construction-time route-order test asserts it).

Recoverable typed errors
------------------------

* :class:`~meho_backplane.topology.bulk_import.BulkImportValidationError`
  re-renders the bulk panel with **every** bad row's error together (the
  REST 422 ``invalid_bulk`` surface) so the operator fixes the file in one
  pass rather than walking it N times.
* A malformed paste / upload (not YAML, not an ``edges:`` list, a row that
  is not a ``{from, kind, to}`` mapping) re-renders the panel with a typed
  parse-error banner before any service call runs.
* :class:`~meho_backplane.targets.resolver.TargetNotFoundError` re-renders
  the refresh result with the 404 near-miss hint (the candidate target
  names) rather than an empty 200.

Tenant isolation
----------------

Every batch derives ``tenant_id`` from the reconstructed
:class:`~meho_backplane.auth.operator.Operator` (validated against the
session) — never a form / query field. ``bulk_import_edges`` resolves every
endpoint under ``operator.tenant_id``; ``resolve_target`` scopes the refresh
target to it, so a cross-tenant target name is indistinguishable from a
missing one (404-class with near-misses drawn only from the caller's tenant).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import GraphEdgeKind
from meho_backplane.targets.resolver import (
    AmbiguousTargetError,
    TargetNotFoundError,
    resolve_target,
)
from meho_backplane.topology.bulk_import import (
    BulkImportValidationError,
    bulk_import_edges,
)
from meho_backplane.topology.refresh import refresh_target_topology
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.csrf import mint_csrf_token
from meho_backplane.ui.routes.approvals.render import set_csrf_cookie
from meho_backplane.ui.routes.connectors.operator import lift_operator_from_session
from meho_backplane.ui.routes.topology.batch_parse import (
    BULK_MAX_ROWS,
    BulkParseError,
    parse_bulk_rows,
    resolve_bulk_text,
)
from meho_backplane.ui.routes.topology.edges import _require_topology_admin
from meho_backplane.ui.templating import get_templates

__all__ = ["build_batch_router"]

_log = structlog.get_logger(__name__)

#: ``HX-Trigger`` event the graph data-island listens for (``... from:body``
#: in ``_graph_data_island.html``). Reused from T1 so a bulk-apply / refresh
#: that changed the graph re-pulls it without a hard reload. Imported as a
#: literal (rather than from :mod:`edges`) to avoid coupling to a private
#: name; the value MUST match the edges module's ``_EDGE_CHANGED_EVENT``.
_EDGE_CHANGED_EVENT = "meho:topology-edge-changed"

#: Upper bound on the pasted / uploaded ``edges:`` doc. The CSRF
#: middleware's body cap only runs for ``application/x-www-form-urlencoded``
#: requests; a multipart upload is bounded by Starlette's
#: ``MultiPartParser`` instead. This tighter app-level cap rejects an
#: oversized paste / upload before the YAML parse allocates, matching the
#: ``targets.yaml`` import surface's ``_YAML_MAX_BYTES``.
_BULK_MAX_BYTES = 256 * 1024

#: The closed edge-kind vocabulary surfaced to the panel (single source of
#: truth — a future widening of :class:`GraphEdgeKind` lands in the panel's
#: hint list for free).
_EDGE_KIND_OPTIONS = [k.value for k in GraphEdgeKind]


# ---------------------------------------------------------------------------
# Module-level Depends closures (ruff B008 guard)
# ---------------------------------------------------------------------------

_require_session = Depends(require_ui_session)


async def _require_topology_operator(
    request: Request,
    session_ctx: UISessionContext = _require_session,
) -> Operator:
    """FastAPI dependency: lift the operator + assert at least ``operator``.

    Refresh is an ``operator``-tier verb (non-destructive rediscovery), so
    this gate accepts both ``operator`` and ``tenant_admin`` (admin is a
    superset). It mirrors
    :func:`~meho_backplane.ui.routes.connectors.operator.resolve_operator_or_403`'s
    JWT-revalidated lift but without the admin narrowing — the refresh route
    must succeed for a plain operator (the RBAC split the issue pins). The
    JWT round-trip catches a same-session role demotion the next time the
    operator acts.
    """
    operator = await lift_operator_from_session(session_ctx)
    if operator.tenant_role not in (TenantRole.OPERATOR, TenantRole.TENANT_ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="topology refresh requires operator",
        )
    return operator


# ---------------------------------------------------------------------------
# Bulk-import panel render helpers
# ---------------------------------------------------------------------------


def _bulk_panel_context(
    *,
    csrf_token: str,
    parse_error: str | None = None,
    row_errors: list[dict[str, object]] | None = None,
    submitted_rows: str = "",
) -> dict[str, object]:
    """Assemble the bulk-import panel context.

    The ``parse_error`` / ``row_errors`` keys let the POST handler re-render
    the panel with a typed banner (parse failure) or the aggregated per-row
    ``invalid_bulk`` list without a second round trip. ``submitted_rows``
    echoes the operator's pasted text back into the textarea so a
    re-submission after a fix does not start from a blank slate.
    """
    return {
        "csrf_token": csrf_token,
        "edge_kind_options": _EDGE_KIND_OPTIONS,
        "parse_error": parse_error,
        "row_errors": row_errors or [],
        "submitted_rows": submitted_rows,
        "max_rows": BULK_MAX_ROWS,
    }


def _render_bulk_panel(
    request: Request,
    *,
    session_id: Any,
    status_code: int = 200,
    parse_error: str | None = None,
    row_errors: list[dict[str, object]] | None = None,
    submitted_rows: str = "",
) -> HTMLResponse:
    """Render ``topology/_bulk_import_modal.html`` + (re)set the CSRF cookie.

    Rotates the CSRF token and re-sets the ``meho_csrf`` cookie on the same
    response so the form's echoed token always lines up with the cookie
    after the HTMX swap (the cookie-desync class T1's modal also guards
    against). Used for the initial panel-open GET and for the typed-error
    re-render on a failed POST.
    """
    csrf_token = mint_csrf_token(str(session_id))
    response = get_templates().TemplateResponse(
        request,
        "topology/_bulk_import_modal.html",
        _bulk_panel_context(
            csrf_token=csrf_token,
            parse_error=parse_error,
            row_errors=row_errors,
            submitted_rows=submitted_rows,
        ),
        status_code=status_code,
    )
    set_csrf_cookie(response, csrf_token)
    return response


def _validation_error_rows(exc: BulkImportValidationError) -> list[dict[str, object]]:
    """Project the service's per-row errors into the panel's template shape.

    Mirrors the REST 422 ``invalid_bulk`` envelope (one entry per failed
    row, every row surfaced together) so the operator fixes the file in one
    pass. The ``kinds`` list (populated for ``invalid_kind`` /
    ``ambiguous_node``) lets the panel show the valid / candidate kinds
    inline.
    """
    return [
        {
            "index": e.index,
            "error": e.error,
            "message": e.message,
            "name": e.name,
            "kind": e.kind,
            "kinds": e.kinds or [],
        }
        for e in exc.errors
    ]


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_batch_router() -> APIRouter:
    """Construct the bulk-import + refresh :class:`APIRouter`.

    Factory function (not a module-level constant) so a test app can
    construct parallel routers without shared route state — the convention
    every topology / approvals / connectors surface router follows. The
    literal ``/ui/topology/edges/bulk`` and the ``/ui/topology/refresh/{target_name}``
    routes are registered through this router, which
    :func:`~meho_backplane.ui.routes.topology.build_router` includes
    **before** ``build_detail_router()`` so neither is shadowed by the
    ``/ui/topology/node/{node_id}`` param route.
    """
    router = APIRouter(tags=["ui-topology"])

    _require_admin = Depends(_require_topology_admin)
    _require_operator = Depends(_require_topology_operator)

    @router.get("/ui/topology/edges/bulk", response_class=HTMLResponse)
    async def bulk_panel(
        request: Request,
        session_ctx: UISessionContext = _require_session,
        # Hard-gate the panel-open too: an operator never reaches the bulk
        # form (defense in depth on top of the table-page soft-hide).
        operator: Operator = _require_admin,
    ) -> HTMLResponse:
        """``GET /ui/topology/edges/bulk`` — render the bulk-import panel."""
        del operator  # gate only; the panel render needs no operator context.
        return _render_bulk_panel(request, session_id=session_ctx.session_id)

    @router.post("/ui/topology/edges/bulk", response_class=HTMLResponse)
    async def bulk_import(
        request: Request,
        session_ctx: UISessionContext = _require_session,
        operator: Operator = _require_admin,
        rows_text: str | None = Form(default=None, max_length=_BULK_MAX_BYTES),
        upload: UploadFile | None = File(default=None),
        dry_run: bool = Form(default=False),
    ) -> HTMLResponse:
        """``POST /ui/topology/edges/bulk`` — preview (dry-run) or apply rows."""
        return await _bulk_import(
            request,
            session_ctx=session_ctx,
            operator=operator,
            pasted=rows_text,
            upload=upload,
            dry_run=dry_run,
        )

    @router.post("/ui/topology/refresh/{target_name}", response_class=HTMLResponse)
    async def refresh(
        request: Request,
        target_name: str,
        operator: Operator = _require_operator,
    ) -> HTMLResponse:
        """``POST /ui/topology/refresh/{target_name}`` — rediscover a target."""
        return await _refresh(request, operator=operator, target_name=target_name)

    return router


# ---------------------------------------------------------------------------
# Bulk-import handler
# ---------------------------------------------------------------------------


async def _bulk_import(
    request: Request,
    *,
    session_ctx: UISessionContext,
    operator: Operator,
    pasted: str | None,
    upload: UploadFile | None,
    dry_run: bool,
) -> HTMLResponse:
    """Parse the rows + run the batch (preview or apply); render the outcome.

    The flow:

    1. Resolve the rows text (upload precedence), parse it into
       :class:`BulkImportRow` objects. A parse failure re-renders the panel
       with a typed banner (422) before any service call.
    2. Call :func:`bulk_import_edges` with the resolved ``dry_run`` flag.
       ``dry_run=True`` returns the per-row ``create`` / ``update`` /
       ``conflict`` plan and persists nothing; ``dry_run=False`` applies the
       batch all-or-nothing and fires the graph-refresh ``HX-Trigger`` so an
       open graph view re-pulls.
    3. A :class:`BulkImportValidationError` re-renders the panel with
       **every** bad row's error together (the 422 ``invalid_bulk`` surface).

    The service takes a session with **no active transaction** (it opens its
    own ``session.begin()`` blocks), so this uses a plain
    ``async with sessionmaker() as db_session`` — no outer ``begin()`` — the
    same lifecycle the REST handler's ``get_raw_session`` dependency yields.
    """
    text = await resolve_bulk_text(pasted, upload)
    try:
        rows = parse_bulk_rows(text)
    except BulkParseError as exc:
        return _render_bulk_panel(
            request,
            session_id=session_ctx.session_id,
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            parse_error=str(exc),
            submitted_rows=text,
        )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db_session:
        try:
            result = await bulk_import_edges(db_session, operator, rows, dry_run=dry_run)
        except BulkImportValidationError as exc:
            return _render_bulk_panel(
                request,
                session_id=session_ctx.session_id,
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                row_errors=_validation_error_rows(exc),
                submitted_rows=text,
            )

    _log.info(
        "ui_topology_bulk_import",
        dry_run=result.dry_run,
        created=result.created,
        updated=result.updated,
        conflicts=result.conflicts,
        rows=len(result.rows),
        operator_sub=operator.sub,
        tenant_id=str(operator.tenant_id),
    )

    if result.dry_run:
        # Preview only: render the per-row plan; persist nothing, fire no
        # graph-refresh trigger (the graph is unchanged).
        return get_templates().TemplateResponse(
            request,
            "topology/_bulk_import_preview.html",
            {"result": result},
        )

    # Apply: render the applied summary + fire the graph-refresh trigger so
    # an open graph view re-pulls the newly-written edges. Only fire when the
    # batch actually wrote something (an all-``update`` no-op batch leaves the
    # graph byte-identical, but re-pulling is harmless and keeps the trigger
    # path uniform with T1's annotate / unannotate).
    response = get_templates().TemplateResponse(
        request,
        "topology/_bulk_import_applied.html",
        {"result": result},
    )
    response.headers["HX-Trigger"] = _EDGE_CHANGED_EVENT
    return response


# ---------------------------------------------------------------------------
# Refresh handler
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RefreshNearMiss:
    """One near-miss target name surfaced on a refresh 404."""

    name: str


async def _refresh(
    request: Request,
    *,
    operator: Operator,
    target_name: str,
) -> HTMLResponse:
    """Resolve the target + rediscover its topology; render the outcome.

    Resolves ``target_name`` tenant-scoped via :func:`resolve_target`, then
    calls the **synchronous** :func:`refresh_target_topology` in-process (it
    blocks on the connector probe and returns a
    :class:`~meho_backplane.topology.refresh.RefreshResult` inline). On
    success renders all six disjoint counts + ``duration_ms`` and fires the
    graph-refresh ``HX-Trigger`` so an open graph view re-pulls. An unknown
    target re-renders the 404 near-miss hint (the candidate names drawn from
    the caller's tenant) rather than an empty 200.

    Uses a session with no active transaction (``resolve_target`` reads
    without an explicit txn; ``refresh_target_topology`` opens its own
    reconcile transaction) — the same lifecycle the REST refresh handler's
    ``get_raw_session`` dependency yields.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db_session:
        try:
            target = await resolve_target(db_session, operator.tenant_id, target_name)
        except TargetNotFoundError as exc:
            return _render_refresh_not_found(
                request, target_name=target_name, exc=exc, status_code=404
            )
        except AmbiguousTargetError as exc:
            # Defensive: the unique index normally prevents this. Surface the
            # candidate names so the operator can disambiguate.
            return _render_refresh_not_found(
                request, target_name=target_name, exc=exc, status_code=409
            )
        result = await refresh_target_topology(target, operator)

    _log.info(
        "ui_topology_refresh",
        target_id=str(result.target_id),
        target_name=target_name,
        added_nodes=result.added_nodes,
        added_edges=result.added_edges,
        updated_nodes=result.updated_nodes,
        updated_edges=result.updated_edges,
        removed_nodes=result.removed_nodes,
        removed_edges=result.removed_edges,
        operator_sub=operator.sub,
        tenant_id=str(operator.tenant_id),
    )

    changed = bool(
        result.added_nodes
        or result.added_edges
        or result.updated_nodes
        or result.updated_edges
        or result.removed_nodes
        or result.removed_edges
    )
    response = get_templates().TemplateResponse(
        request,
        "topology/_refresh_result.html",
        {"result": result, "target_name": target_name},
    )
    if changed:
        # Only re-pull the graph when the reconcile actually changed it; an
        # unchanged refresh (last_seen bump only) leaves the rendered graph
        # identical, so the trigger would be a wasted round trip.
        response.headers["HX-Trigger"] = _EDGE_CHANGED_EVENT
    return response


def _render_refresh_not_found(
    request: Request,
    *,
    target_name: str,
    exc: TargetNotFoundError | AmbiguousTargetError,
    status_code: int,
) -> HTMLResponse:
    """Render the refresh 404 / 409 fragment with the near-miss target names.

    Both resolver errors carry a structured ``detail`` whose ``matches`` is a
    list of ``TargetSummary`` dicts (the prefix-ILIKE near-misses on a 404,
    the colliding rows on a 409). The fragment lists the candidate names so
    the operator can re-submit with an exact name rather than seeing a dead
    error.
    """
    matches = _extract_match_names(exc)
    return get_templates().TemplateResponse(
        request,
        "topology/_refresh_not_found.html",
        {
            "target_name": target_name,
            "near_misses": [_RefreshNearMiss(name=name) for name in matches],
            "is_ambiguous": status_code == 409,
        },
        status_code=status_code,
    )


def _extract_match_names(exc: TargetNotFoundError | AmbiguousTargetError) -> list[str]:
    """Pull the candidate target names out of a resolver error's ``detail``.

    The resolver builds ``detail={"matches": [<TargetSummary json>, ...]}``;
    each summary carries a ``name``. Reads defensively (the detail shape is
    the resolver's contract, but a future change should not 500 the panel).
    """
    detail = getattr(exc, "detail", None)
    if not isinstance(detail, dict):
        return []
    matches = detail.get("matches")
    if not isinstance(matches, list):
        return []
    names: list[str] = []
    for match in matches:
        if isinstance(match, dict):
            name = match.get("name")
            if isinstance(name, str):
                names.append(name)
    return names
