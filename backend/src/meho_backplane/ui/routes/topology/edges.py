# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Curated-edge **write** routes for the topology console (in-process BFF).

Initiative #1941 (G10.17 Topology console writes), Task #1953 (T1). The
read-only topology surface (table + Cytoscape graph + node drawer, #880 /
#881 / #882) carries no write verbs; the curated-edge annotate / unannotate
verbs exist only on the CLI (``meho topology annotate`` / ``unannotate``)
and the Bearer REST API (``POST`` / ``DELETE /api/v1/topology/edges``). This
module is the operator-console face of those verbs.

Why a session BFF and not the Bearer ``/api/v1/topology/edges`` routes
---------------------------------------------------------------------

The REST edge routes are Bearer-gated (``_require_admin`` over a verified
JWT). A browser carrying only the BFF session cookie + the CSRF
double-submit token cannot authenticate them. So this module adds
``/ui/topology/edges*`` sub-routes that are ``require_ui_session`` +
CSRF-gated and call the
:mod:`~meho_backplane.topology.annotate` **service** in-process
(:func:`~meho_backplane.topology.annotate.annotate_edge` /
:func:`~meho_backplane.topology.annotate.unannotate_edge`) — the same
console-surface pattern the approvals / connectors surfaces use. The
in-process call keeps the synchronous-audit binding and avoids a self-HTTP
hop the cookie could not auth anyway.

Route inventory
---------------

* ``GET /ui/topology/edges/annotate`` — the annotate-modal fragment. The
  literal ``edges`` / ``annotate`` segments register **ahead of**
  ``detail.py``'s ``/ui/topology/node/{node_id}`` param route so the
  first-match-wins lookup never binds them as a node id (verified at
  construction time with a FastAPI test client). The modal mints + sets a
  fresh ``meho_csrf`` cookie so the POST carries a valid double-submit
  token.
* ``POST /ui/topology/edges`` — CSRF-gated. Reconstructs the operator,
  builds two :class:`~meho_backplane.topology.annotate.NodeRef` endpoints
  (``from`` is a Python-reserved word, so the wire field never carries a
  raw ``from`` key — the handler constructs the ``NodeRef`` directly), and
  calls :func:`annotate_edge` in-process. Renders the created edge inline +
  fires an ``HX-Trigger`` so an open graph view re-pulls.
* ``DELETE /ui/topology/edges/{edge_id}`` — CSRF-gated, confirm-gated.
  Calls :func:`unannotate_edge` in-process; on success swaps the row out +
  fires the same graph-refresh trigger. The ``{edge_id}`` param route is a
  ``DELETE``, so it never collides with the ``GET`` annotate-modal route.

RBAC tier split (load-bearing)
------------------------------

Curated-edge writes are **``tenant_admin``** at the REST layer (an
annotation can shrink the auto-flagged blast radius of an op the operator
then runs — the same gate as ``POST /api/v1/targets``). The BFF mirrors
this two ways:

* **Soft-hide** — the drawer (``_drawer.html``) renders the annotate /
  remove controls only when ``is_tenant_admin`` (projected via
  :func:`~meho_backplane.ui.routes.connectors.operator.resolve_role_probe`,
  fail-soft). An operator never sees the affordances.
* **Hard-403** — every route here gates on
  :func:`_require_topology_admin` (mirrors
  :func:`~meho_backplane.ui.routes.connectors.operator.resolve_operator_or_403`)
  so a forged POST from a non-admin session is rejected server-side. The
  same template serving both roles is the regression trap — the disabled /
  absent button is UX only; the gate is the authority.

Recoverable typed errors
-------------------------

The service raises HTTP-agnostic ``ValueError`` subclasses; this module
maps each to a re-rendered fragment with a legible banner rather than a
dead 5xx, mirroring the REST layer's status codes:

* :class:`~meho_backplane.topology.resolvers.AmbiguousNodeError` → re-render
  the annotate modal with the candidate ``kinds`` listed so the operator
  re-submits with a disambiguating ``kind`` (the REST 409 ``ambiguous_node``
  surface).
* :class:`~meho_backplane.topology.resolvers.NodeNotFoundError` /
  :class:`~meho_backplane.topology.annotate.InvalidEdgeKindError` → re-render
  the modal with the typed error banner.
* :class:`~meho_backplane.topology.annotate.AutoEdgeDeletionError` →
  re-render the row with the recoverable "this is an auto edge; annotate
  over it first" banner (the REST 409 ``auto_edge_deletion`` surface). Auto
  edges resurrect on the next refresh, so a naive DELETE retry would
  confuse the operator.

Tenant isolation
----------------

Every write derives ``tenant_id`` from the reconstructed
:class:`~meho_backplane.auth.operator.Operator` (itself validated against
the session) — never a form / query field. The service's endpoint resolver
scopes on ``operator.tenant_id``, so a cross-tenant node name is
indistinguishable from a missing one (404-class), and a cross-tenant
``edge_id`` is indistinguishable from a missing one (the
``unannotate_edge`` tenant-boundary contract).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import GraphEdge, GraphEdgeKind, GraphNode
from meho_backplane.topology.annotate import (
    AutoEdgeDeletionError,
    InvalidEdgeKindError,
    NodeRef,
    annotate_edge,
    unannotate_edge,
)
from meho_backplane.topology.resolvers import AmbiguousNodeError, NodeNotFoundError
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.csrf import mint_csrf_token
from meho_backplane.ui.routes.approvals.render import set_csrf_cookie
from meho_backplane.ui.routes.connectors.operator import lift_operator_from_session
from meho_backplane.ui.templating import get_templates

__all__ = ["build_edges_router"]

_log = structlog.get_logger(__name__)

#: ``HX-Trigger`` event name fired on a successful annotate / unannotate.
#: The graph view's data-island wrapper (``topology-graph.js``) listens for
#: it and re-pulls the graph JSON so a just-written / removed edge shows
#: without a hard reload (the issue's "the graph reflects writes" criterion).
_EDGE_CHANGED_EVENT = "meho:topology-edge-changed"

#: Bound on the operator-supplied free-text fields, matching the REST
#: ``_AnnotateEdgeRequest`` (``note`` / ``evidence_url`` ``max_length=2000``)
#: so an oversized paste is rejected at the form boundary rather than
#: forwarded into the service.
_MAX_NOTE_LENGTH = 2000
_MAX_EVIDENCE_URL_LENGTH = 2000

#: The closed v0.2 edge-kind vocabulary, surfaced to the modal's ``kind``
#: dropdown. Sourced from the enum (single source of truth) so a future
#: widening of :class:`GraphEdgeKind` lands in the modal for free.
_EDGE_KIND_OPTIONS = [k.value for k in GraphEdgeKind]


@dataclass(frozen=True)
class _EdgeEndpointRow:
    """Compact endpoint shape the created-edge fragment renders.

    Mirrors :class:`meho_backplane.ui.routes.topology.detail._EdgeEndpointRow`
    so the inline created-edge row and the drawer's neighbour list share a
    template-facing shape.
    """

    id: uuid.UUID
    kind: str
    name: str


@dataclass(frozen=True)
class _EdgeRow:
    """One curated edge rendered inline after a successful annotate.

    Built from the :class:`GraphEdge` row :func:`annotate_edge` returns
    (plus a re-fetch of the two endpoint nodes for their human-readable
    ``(kind, name)``). Matches the drawer's ``_EdgeRow`` field shape so the
    created-edge fragment can reuse the drawer's edge-row markup.
    """

    id: uuid.UUID
    kind: str
    source: str
    from_endpoint: _EdgeEndpointRow
    to_endpoint: _EdgeEndpointRow


@dataclass(frozen=True)
class _AnnotateForm:
    """The annotate POST's form fields, bundled so they pass as one value.

    Keeps the ``POST /ui/topology/edges`` handler and the in-process
    annotate helper from threading seven discrete arguments each (the
    ``from`` / ``kind`` / ``to`` / ``note`` / ``evidence_url`` wire shape).
    The ``submitted`` projection echoes the operator's input back into the
    modal on a re-render after a disambiguation hint.
    """

    from_name: str
    from_kind: str
    kind: str
    to_name: str
    to_kind: str
    note: str
    evidence_url: str

    def submitted(self) -> dict[str, str]:
        """The form fields as a template dict for the modal-re-render echo."""
        return {
            "from_name": self.from_name,
            "from_kind": self.from_kind,
            "kind": self.kind,
            "to_name": self.to_name,
            "to_kind": self.to_kind,
            "note": self.note,
            "evidence_url": self.evidence_url,
        }


#: Module-level ``Depends`` closures (ruff B008 guard), matching the
#: convention every UI surface router follows.
_require_session = Depends(require_ui_session)


async def _require_topology_admin(
    request: Request,
    session_ctx: UISessionContext = _require_session,
) -> Operator:
    """FastAPI dependency: lift the operator + hard-gate to ``tenant_admin``.

    Mirrors
    :func:`~meho_backplane.ui.routes.connectors.operator.resolve_operator_or_403`
    but raises a topology-specific 403 detail. Used by every write route
    here (the annotate-modal GET, the annotate POST, the unannotate DELETE)
    so a non-admin session — even one that forged the request past the
    soft-hidden controls — is rejected server-side. The role probe used for
    the drawer's soft-hide is UX only; this gate is the authority.
    """
    operator = await lift_operator_from_session(session_ctx)
    if operator.tenant_role != TenantRole.TENANT_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="curated-edge writes require tenant_admin",
        )
    return operator


async def _load_edge_row(session: AsyncSession, *, edge: GraphEdge) -> _EdgeRow:
    """Project a freshly-written :class:`GraphEdge` into the inline row shape.

    Re-fetches the two endpoint nodes (the ORM row carries only the FK ids)
    so the created-edge fragment can render the human-readable
    ``(kind, name)`` for each side. Tenant scoping is already guaranteed —
    the edge was written under ``operator.tenant_id`` and both endpoints
    resolved within that tenant — so this read does not re-assert it.
    """
    from_alias = aliased(GraphNode)
    to_alias = aliased(GraphNode)
    stmt = (
        select(
            from_alias.id.label("from_id"),
            from_alias.kind.label("from_kind"),
            from_alias.name.label("from_name"),
            to_alias.id.label("to_id"),
            to_alias.kind.label("to_kind"),
            to_alias.name.label("to_name"),
        )
        .select_from(GraphEdge)
        .join(from_alias, from_alias.id == GraphEdge.from_node_id)
        .join(to_alias, to_alias.id == GraphEdge.to_node_id)
        .where(GraphEdge.id == edge.id)
    )
    result = await session.execute(stmt)
    row = result.one()
    return _EdgeRow(
        id=edge.id,
        kind=edge.kind,
        source=edge.source,
        from_endpoint=_EdgeEndpointRow(id=row.from_id, kind=row.from_kind, name=row.from_name),
        to_endpoint=_EdgeEndpointRow(id=row.to_id, kind=row.to_kind, name=row.to_name),
    )


def _annotate_modal_context(
    *,
    csrf_token: str,
    error_message: str | None = None,
    ambiguous_name: str | None = None,
    ambiguous_kinds: list[str] | None = None,
    submitted: dict[str, str] | None = None,
) -> dict[str, object]:
    """Assemble the annotate-modal context dict.

    The ``error_*`` / ``ambiguous_*`` keys let the POST handler re-render
    the modal with a typed banner (and, for an ambiguous node, the
    candidate ``kinds``) without a second round trip. ``submitted`` echoes
    the operator's prior input back into the form fields so a re-submission
    after a disambiguation hint does not start from a blank slate.
    """
    return {
        "csrf_token": csrf_token,
        "edge_kind_options": _EDGE_KIND_OPTIONS,
        "error_message": error_message,
        "ambiguous_name": ambiguous_name,
        "ambiguous_kinds": ambiguous_kinds or [],
        "submitted": submitted or {},
        "max_note_length": _MAX_NOTE_LENGTH,
        "max_evidence_url_length": _MAX_EVIDENCE_URL_LENGTH,
    }


def _render_annotate_modal(
    request: Request,
    *,
    session_id: uuid.UUID,
    status_code: int = 200,
    error_message: str | None = None,
    ambiguous_name: str | None = None,
    ambiguous_kinds: list[str] | None = None,
    submitted: dict[str, str] | None = None,
) -> HTMLResponse:
    """Render ``topology/_annotate_modal.html`` + (re)set the CSRF cookie.

    The modal render rotates the CSRF token and re-sets the ``meho_csrf``
    cookie on the same response so the form's echoed token always lines up
    with the cookie after the HTMX swap (the cookie-desync class the
    approvals modal also guards against). Used both for the initial
    modal-open GET and for the typed-error re-render on a failed POST.
    """
    csrf_token = mint_csrf_token(str(session_id))
    context = _annotate_modal_context(
        csrf_token=csrf_token,
        error_message=error_message,
        ambiguous_name=ambiguous_name,
        ambiguous_kinds=ambiguous_kinds,
        submitted=submitted,
    )
    response = get_templates().TemplateResponse(
        request,
        "topology/_annotate_modal.html",
        context,
        status_code=status_code,
    )
    set_csrf_cookie(response, csrf_token)
    return response


def _node_ref(name: str, kind: str) -> NodeRef:
    """Build a :class:`NodeRef` from a form ``name`` + optional ``kind``.

    A blank ``kind`` (the operator left the optional disambiguator empty)
    becomes ``None`` so the resolver runs its bare-name branch — which may
    raise :class:`AmbiguousNodeError` the POST handler surfaces. ``from`` is
    never posted as a raw JSON key; the handler always reaches the service
    through this constructor.
    """
    return NodeRef(name=name.strip(), kind=kind.strip() or None)


def build_edges_router() -> APIRouter:
    """Construct the ``/ui/topology/edges*`` write :class:`APIRouter`.

    Factory function (not a module-level constant) so a test app can
    construct parallel routers without shared route state — the convention
    every topology / approvals / connectors surface router follows. The
    literal ``edges`` / ``annotate`` segments are registered through this
    router, which :func:`meho_backplane.ui.routes.topology.build_router`
    includes **before** ``build_detail_router()`` so the
    ``/ui/topology/node/{node_id}`` param route never shadows them.
    """
    router = APIRouter(tags=["ui-topology"])

    _require_admin = Depends(_require_topology_admin)

    @router.get("/ui/topology/edges/annotate", response_class=HTMLResponse)
    async def annotate_modal(
        request: Request,
        session_ctx: UISessionContext = _require_session,
        # Hard-gate the modal-open too: an operator never reaches the
        # annotate form (defense in depth on top of the drawer soft-hide).
        operator: Operator = _require_admin,
    ) -> HTMLResponse:
        """``GET /ui/topology/edges/annotate`` — render the annotate modal."""
        return _render_annotate_modal(request, session_id=session_ctx.session_id)

    @router.post("/ui/topology/edges", response_class=HTMLResponse)
    async def annotate(
        request: Request,
        session_ctx: UISessionContext = _require_session,
        operator: Operator = _require_admin,
        from_name: str = Form(..., min_length=1),
        from_kind: str = Form(default=""),
        kind: str = Form(..., min_length=1),
        to_name: str = Form(..., min_length=1),
        to_kind: str = Form(default=""),
        note: str = Form(default="", max_length=_MAX_NOTE_LENGTH),
        evidence_url: str = Form(default="", max_length=_MAX_EVIDENCE_URL_LENGTH),
    ) -> HTMLResponse:
        """``POST /ui/topology/edges`` — annotate a curated edge in-process."""
        return await _annotate(
            request,
            session_ctx=session_ctx,
            operator=operator,
            form=_AnnotateForm(
                from_name=from_name,
                from_kind=from_kind,
                kind=kind,
                to_name=to_name,
                to_kind=to_kind,
                note=note,
                evidence_url=evidence_url,
            ),
        )

    @router.delete("/ui/topology/edges/{edge_id}", response_class=HTMLResponse)
    async def unannotate(
        request: Request,
        edge_id: uuid.UUID,
        operator: Operator = _require_admin,
    ) -> HTMLResponse:
        """``DELETE /ui/topology/edges/{edge_id}`` — remove a curated edge."""
        return await _unannotate(request, operator=operator, edge_id=edge_id)

    return router


def _annotate_error_modal(
    request: Request,
    *,
    session_id: uuid.UUID,
    exc: AmbiguousNodeError | NodeNotFoundError | InvalidEdgeKindError,
    submitted: dict[str, str],
) -> HTMLResponse:
    """Map a recoverable annotate failure to a modal re-render.

    Mirrors the REST layer's status codes
    (:mod:`meho_backplane.api.v1.topology`): 409 ``ambiguous_node`` (with
    the candidate ``kinds`` echoed so the operator re-submits with a
    disambiguating ``kind``), 404 for an unknown node, 422 for an
    out-of-vocabulary edge kind. The modal stays open with the typed banner
    rather than tearing the operator out of the flow.
    """
    if isinstance(exc, AmbiguousNodeError):
        return _render_annotate_modal(
            request,
            session_id=session_id,
            status_code=status.HTTP_409_CONFLICT,
            error_message=(
                f"The name {exc.name!r} is ambiguous in this tenant — it exists as "
                "more than one kind. Pick a kind below and re-submit."
            ),
            ambiguous_name=exc.name,
            ambiguous_kinds=sorted(exc.kinds),
            submitted=submitted,
        )
    if isinstance(exc, NodeNotFoundError):
        return _render_annotate_modal(
            request,
            session_id=session_id,
            status_code=status.HTTP_404_NOT_FOUND,
            error_message=f"No node matched {exc.name!r} in this tenant.",
            submitted=submitted,
        )
    return _render_annotate_modal(
        request,
        session_id=session_id,
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        error_message=(
            f"Edge kind {exc.kind!r} is not in the v0.2 vocabulary. Pick a valid kind below."
        ),
        submitted=submitted,
    )


async def _annotate(
    request: Request,
    *,
    session_ctx: UISessionContext,
    operator: Operator,
    form: _AnnotateForm,
) -> HTMLResponse:
    """Resolve endpoints + upsert the curated edge; render the outcome.

    On success renders the created edge inline and fires the
    :data:`_EDGE_CHANGED_EVENT` ``HX-Trigger`` so an open graph view
    re-pulls. A recoverable failure (ambiguous node, unknown node, invalid
    kind) re-renders the annotate modal with the typed banner via
    :func:`_annotate_error_modal` rather than tearing the operator out of
    the flow.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db_session:
        try:
            edge = await annotate_edge(
                db_session,
                operator,
                _node_ref(form.from_name, form.from_kind),
                form.kind,
                _node_ref(form.to_name, form.to_kind),
                note=form.note.strip() or None,
                evidence_url=form.evidence_url.strip() or None,
            )
        except (AmbiguousNodeError, NodeNotFoundError, InvalidEdgeKindError) as exc:
            return _annotate_error_modal(
                request,
                session_id=session_ctx.session_id,
                exc=exc,
                submitted=form.submitted(),
            )
        edge_row = await _load_edge_row(db_session, edge=edge)

    _log.info(
        "ui_topology_edge_annotated",
        edge_id=str(edge_row.id),
        kind=edge_row.kind,
        operator_sub=operator.sub,
        tenant_id=str(operator.tenant_id),
    )
    response = get_templates().TemplateResponse(
        request,
        "topology/_edge_created.html",
        {"edge": edge_row},
    )
    response.headers["HX-Trigger"] = _EDGE_CHANGED_EVENT
    return response


async def _unannotate(
    request: Request,
    *,
    operator: Operator,
    edge_id: uuid.UUID,
) -> HTMLResponse:
    """Hard-delete the curated edge; render the outcome.

    On success swaps the row out (an empty fragment + an ``HX-Trigger`` so
    an open graph view re-pulls). An ``auto_edge_deletion`` refusal
    re-renders a recoverable banner ("this is an auto edge; annotate over it
    first") rather than a dead error — auto edges resurrect on the next
    refresh, so a naive DELETE retry would confuse the operator. A
    missing / cross-tenant id renders a not-found banner.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db_session:
        try:
            await unannotate_edge(db_session, operator, edge_id=edge_id)
        except AutoEdgeDeletionError as exc:
            return get_templates().TemplateResponse(
                request,
                "topology/_edge_remove_error.html",
                {
                    "edge_id": str(exc.edge_id),
                    "error_kind": "auto_edge_deletion",
                    "error_message": (
                        "This is an auto-discovered edge; auto edges resurrect on the "
                        "next refresh, so removing it is a no-op. Annotate over the auto "
                        "edge first, then remove the curated row."
                    ),
                },
                status_code=status.HTTP_409_CONFLICT,
            )
        except ValueError:
            # The id-form selector raises ValueError when the row is not in
            # this tenant (cross-tenant ids are indistinguishable from
            # missing ones — the tenant-boundary contract). Mirrors the REST
            # 404 ``edge_not_found`` surface.
            return get_templates().TemplateResponse(
                request,
                "topology/_edge_remove_error.html",
                {
                    "edge_id": str(edge_id),
                    "error_kind": "edge_not_found",
                    "error_message": f"No edge {edge_id} found in this tenant.",
                },
                status_code=status.HTTP_404_NOT_FOUND,
            )

    _log.info(
        "ui_topology_edge_unannotated",
        edge_id=str(edge_id),
        operator_sub=operator.sub,
        tenant_id=str(operator.tenant_id),
    )
    response = get_templates().TemplateResponse(
        request,
        "topology/_edge_removed.html",
        {"edge_id": str(edge_id)},
    )
    response.headers["HX-Trigger"] = _EDGE_CHANGED_EVENT
    return response
