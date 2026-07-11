# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/conventions*`` -- REST surface for tenant conventions.

Initiative #229 (G7.1), Task #314 (T2). Six routes mounted under
``/api/v1/conventions`` over the :class:`TenantConvention`
substrate (T1 #313). T3 (#315) layers CLI verbs over this
surface; T4 (#316) layers the session-preamble assembler that
reads through the same Pydantic shapes.

G10.12-T0 (#1894): the CRUD + budget + history + pre-allocated-
audit-id logic now lives in
:class:`~meho_backplane.conventions.service.ConventionsService` so the
in-process console BFF (T1/T2 #1838) can reuse it without copying the
handlers. These handlers are a thin HTTP shell: they bind the audit
contextvars, delegate to the service, and map the service's typed
error vocabulary (:class:`ConventionNotFoundError` / :class:`ConventionConflictError`
/ :class:`OverBudgetError`) onto ``HTTPException`` status codes. The
wire behaviour (status codes, response bodies, audit rows, history
rows) is identical to the pre-extraction inline implementation.

Routes (all tenant-scoped; cross-tenant probes 404, never 403):

* ``GET ``                              list (``?kind=`` filter, operator)
* ``GET  /{slug}``                      show one              (operator)
* ``POST ``                             create                (tenant_admin)
* ``PATCH /{slug}``                     update                (tenant_admin)
* ``DELETE /{slug}``                    delete                (tenant_admin)
* ``GET  /{slug}/history``              history list, newest first (operator)

Write-time 422 (POST + PATCH on ``body`` change against an
``operational`` row): a single convention whose
:func:`~meho_backplane.conventions.schemas.estimate_tokens` cost
exceeds :data:`~meho_backplane.conventions.schemas.DEFAULT_MAX_PREAMBLE_TOKENS`
is rejected with detail naming ``estimated`` vs ``budget``. T4's
preamble assembler reuses the same helper so the two sites
cannot drift -- the failure mode that motivated the gate is a
write passing the API only to be silently dropped at every
future preamble assembly ("``kubectl apply --dry-run=server``
discipline"). ``workflow`` / ``reference`` are not preamble-
bound and exempt.

Audit row + history row commit in the same transaction. The
history row's ``audit_id`` soft-FK matches the chassis
:class:`~meho_backplane.audit.AuditMiddleware` row's ``id`` --
:class:`~meho_backplane.conventions.service.ConventionsService`
pre-allocates the uuid (binding it onto the audit contextvar) and
the middleware honours it; G8's audit-query path joins on that
soft-FK for forensic queries.

Every route binds ``audit_op_id`` / ``audit_op_class`` /
``audit_slug`` contextvars before the DB probe so the audit row
classifies correctly even when the handler raises.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.conventions.schemas import (
    Convention,
    ConventionCreate,
    ConventionHistoryEntry,
    ConventionKind,
    ConventionListResponse,
    ConventionUpdate,
)
from meho_backplane.conventions.service import (
    ConventionConflictError,
    ConventionNotFoundError,
    ConventionsService,
    OverBudgetError,
)
from meho_backplane.db.engine import get_session
from meho_backplane.mcp.server import RESOURCES_SUBSCRIBE_ENABLED

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/conventions", tags=["conventions"])


#: Module-level :class:`Depends` closures -- built once at import time
#: to satisfy ruff's B008 rule (function calls in default argument
#: positions are disallowed). Mirrors the convention in
#: :mod:`~meho_backplane.api.v1.memory` and
#: :mod:`~meho_backplane.api.v1.broadcast_overrides`.
_require_operator = Depends(require_role(TenantRole.OPERATOR))
_require_tenant_admin = Depends(require_role(TenantRole.TENANT_ADMIN))

#: Single service instance -- stateless (no held session / connection),
#: so one module-level object is safe to share across requests.
_service = ConventionsService()


#: Canonical operation identifiers bound into ``audit_op_id`` per
#: route. Pinned as module constants so the contract is greppable
#: from tests + G8 dashboards. A typo in a handler surfaces at first
#: call rather than as a silent broadcast under the wrong op_id.
_OP_ID_LIST: Final[str] = "conventions.list"
_OP_ID_SHOW: Final[str] = "conventions.show"
_OP_ID_CREATE: Final[str] = "conventions.create"
_OP_ID_UPDATE: Final[str] = "conventions.update"
_OP_ID_DELETE: Final[str] = "conventions.delete"
_OP_ID_HISTORY: Final[str] = "conventions.history"


#: Maximum length of the ``slug`` path parameter accepted by
#: ``/{slug}`` routes. Defence-in-depth over the substrate's own
#: ``max_length=128`` -- a pathological slug substring in the URL
#: (10 KB of dots, hyphens) is rejected at the path-parameter parse
#: stage before any DB probe runs. Matches the substrate's bound
#: exactly so a request that passes the path-parse also passes the
#: pydantic field validator on the create body.
_SLUG_MAX_LENGTH: Final[int] = 128


def _not_found() -> HTTPException:
    """The consistent 404 the tenant-boundary info-leak avoidance requires."""
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="convention_not_found",
    )


def _over_budget_422(exc: OverBudgetError) -> HTTPException:
    """Map the service's over-budget error to the 422 the wire contract names."""
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=(
            f"convention body exceeds preamble budget "
            f"(estimated={exc.estimated}, budget={exc.budget})"
        ),
    )


def _maybe_emit_resource_updated(
    *,
    tenant_id: uuid.UUID,
    slug: str,
) -> None:
    """Emit ``notifications/resources/updated`` for a convention edit -- iff subscribe is on.

    Gated by :data:`~meho_backplane.mcp.server.RESOURCES_SUBSCRIBE_ENABLED`
    -- the single source of truth for whether the server advertises
    ``capabilities.resources.subscribe`` on ``initialize``. v0.2
    ships ``False`` and this helper is a no-op; a v0.2.next flip of
    the constant lights up the emit code path without touching the
    convention CRUD routes.

    Why the gate matters: emitting the notification while the server
    is also advertising ``subscribe: false`` would tell a spec-
    conforming client *"you can subscribe to resource changes"* via
    the runtime event while the handshake said the opposite. That
    asymmetry violates MCP 2025-06-18 §"Capability Negotiation"
    ("Only use capabilities that were successfully negotiated") and
    risks confused-deputy behaviour on the client side.

    The actual transport for the notification (when the gate flips)
    is out of scope here -- it requires the long-poll / SSE bridge
    deferred to v0.2.next. The helper currently structures the call
    site (one call per write route, one place to add the publish
    when the bridge lands) and writes a debug log so the v0.2.next
    audit trail of "which writes WOULD have emitted" is queryable
    via structlog without needing a code-search pass.

    Per the issue body's MCP-spec citation, the conditional emit
    pairs with the resource URI template registered in
    :mod:`meho_backplane.mcp.resources.tenant_conventions`; the
    ``meho://tenant/{tenant_id}/conventions/{slug}`` URI is what
    appears in the notification's ``uri`` field per
    https://modelcontextprotocol.io/specification/2025-06-18/server/resources.
    """
    if not RESOURCES_SUBSCRIBE_ENABLED:
        # v0.2 posture: ``subscribe: false`` on initialize ->
        # silent no-op here. The branch is intentionally cheap
        # (one constant read) so leaving the call site in place
        # has zero runtime cost.
        return
    # v0.2.next branch (currently dead-but-staged): emit the
    # notification through the SSE/long-poll bridge once it lands.
    # The URI shape MUST match the registered resource template in
    # ``mcp.resources.tenant_conventions`` -- duplicated string
    # literal across two files is the lesser evil vs. cross-module
    # coupling that pulls api/v1 into mcp/resources.
    uri = f"meho://tenant/{tenant_id}/conventions/{slug}"
    structlog.get_logger().info(
        "mcp_resource_updated_emit",
        uri=uri,
        tenant_id=str(tenant_id),
        slug=slug,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/conventions -- list
# ---------------------------------------------------------------------------


@router.get("", response_model=ConventionListResponse)
async def list_conventions(
    operator: Annotated[Operator, _require_operator],
    session: Annotated[AsyncSession, Depends(get_session)],
    kind: Annotated[
        ConventionKind | None,
        Query(description="Filter by kind (operational / workflow / reference)."),
    ] = None,
) -> ConventionListResponse:
    """List the operator's tenant's conventions, optionally filtered by kind.

    Returns the lighter :class:`ConventionSummary` shape (no full
    ``body``). Ordering is ``priority DESC, created_at ASC`` --
    the same key T4's preamble assembler uses for packing, so the
    list view surfaces conventions in T4's consideration order.

    Returns the unified ``{"items": [...], "next_cursor": null,
    "budget_status": {...}}`` list envelope per
    ``docs/codebase/api-shape-conventions.md`` §2 (#2338 breaking pass --
    the response shape converged from the v0.8.0 ``{"entries": [...]}``
    keyed shape onto the reference envelope, renaming ``entries`` ->
    ``items``; the ``?envelope=v2`` opt-in that bridged the migration
    was retired). The convention list is unpaginated, so ``next_cursor``
    is always ``null`` and ``budget_status`` rides as a top-level
    sidecar (not nested under ``meta``).

    ``budget_status`` (T7 #1094) is the preamble budget arithmetic for
    the operator's tenant, computed by a call to
    :func:`~meho_backplane.conventions.preamble.assemble_preamble`. The
    ``--kind`` query filter narrows the ``items`` list only;
    ``budget_status`` always reflects the full ``operational`` set (a
    ``--kind=workflow`` list call still wants the truthful budget
    signal). The assembler runs an indexed SELECT + an in-memory pack --
    O(N) per call where N is the tenant's operational-convention count
    (~10-20 in practice), so the cost is negligible against the list
    round-trip.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_ID_LIST,
        audit_op_class="read",
    )
    entries, budget_status = await _service.list_conventions(
        session=session,
        operator=operator,
        kind=kind,
    )

    return ConventionListResponse(
        items=entries,
        budget_status=budget_status,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/conventions/{slug} -- show one
# ---------------------------------------------------------------------------


@router.get("/{slug}", response_model=Convention)
async def show_convention(
    operator: Annotated[Operator, _require_operator],
    session: Annotated[AsyncSession, Depends(get_session)],
    slug: Annotated[str, Path(max_length=_SLUG_MAX_LENGTH)],
) -> Convention:
    """Fetch one convention by slug. 404 on absent OR cross-tenant.

    The 404-vs-403 collapse on cross-tenant probes preserves the
    tenant-boundary info-leak avoidance contract (the lookup
    filters on both ``tenant_id`` AND ``slug``).
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_ID_SHOW,
        audit_op_class="read",
        audit_slug=slug,
    )
    try:
        return await _service.get_convention(
            session=session,
            tenant_id=operator.tenant_id,
            slug=slug,
        )
    except ConventionNotFoundError as exc:
        raise _not_found() from exc


# ---------------------------------------------------------------------------
# POST /api/v1/conventions -- create
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=Convention,
    status_code=status.HTTP_201_CREATED,
)
async def create_convention(
    body: ConventionCreate,
    operator: Annotated[Operator, _require_tenant_admin],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Convention:
    """Create one convention; writes one history row in the same transaction.

    Sequence: budget 422 -> pre-allocate audit_id -> insert
    convention + history rows -> 409 on duplicate ``(tenant_id,
    slug)``. The CREATE history row has ``body_before=NULL``;
    ``body_after=body.body``.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_ID_CREATE,
        audit_op_class="write",
        audit_slug=body.slug,
    )
    try:
        convention = await _service.create_convention(
            session=session,
            operator=operator,
            body=body,
        )
    except OverBudgetError as exc:
        raise _over_budget_422(exc) from exc
    except ConventionConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"convention {body.slug!r} already exists",
        ) from exc
    # Conditional MCP resources/updated notification (T4 #316).
    # No-op while ``resources.subscribe`` is False; structured emit
    # call site is in place for the v0.2.next subscribe flip.
    _maybe_emit_resource_updated(tenant_id=operator.tenant_id, slug=body.slug)
    return convention


# ---------------------------------------------------------------------------
# PATCH /api/v1/conventions/{slug} -- update
# ---------------------------------------------------------------------------


@router.patch("/{slug}", response_model=Convention)
async def update_convention(
    body: ConventionUpdate,
    operator: Annotated[Operator, _require_tenant_admin],
    session: Annotated[AsyncSession, Depends(get_session)],
    slug: Annotated[str, Path(max_length=_SLUG_MAX_LENGTH)],
) -> Convention:
    """Update one convention; writes one history row in the same transaction.

    PATCH applies only fields explicitly set in the request body
    (via :attr:`BaseModel.model_fields_set` -- the v2 pattern that
    distinguishes "absent" from "present null"). The 422 budget
    gate fires on a ``body`` change; PATCH cannot change ``kind``
    so the check uses the existing row's kind. A priority-only
    or title-only PATCH still writes a history row carrying the
    unchanged body in both ``body_before`` and ``body_after`` --
    the operation happened and the diff trail is the causal record
    (T3's CLI renders an empty diff as "(no body change)").
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_ID_UPDATE,
        audit_op_class="write",
        audit_slug=slug,
    )
    try:
        convention = await _service.update_convention(
            session=session,
            operator=operator,
            slug=slug,
            body=body,
        )
    except ConventionNotFoundError as exc:
        raise _not_found() from exc
    except OverBudgetError as exc:
        raise _over_budget_422(exc) from exc
    # Conditional MCP resources/updated notification (T4 #316).
    # No-op while ``resources.subscribe`` is False; structured emit
    # call site is in place for the v0.2.next subscribe flip.
    _maybe_emit_resource_updated(tenant_id=operator.tenant_id, slug=slug)
    return convention


# ---------------------------------------------------------------------------
# DELETE /api/v1/conventions/{slug} -- delete
# ---------------------------------------------------------------------------


@router.delete(
    "/{slug}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_convention(
    operator: Annotated[Operator, _require_tenant_admin],
    session: Annotated[AsyncSession, Depends(get_session)],
    slug: Annotated[str, Path(max_length=_SLUG_MAX_LENGTH)],
) -> Response:
    """Delete one convention; writes one history row in the same transaction.

    The history row carries ``body_after=<final body>`` -- a
    legible last-known state for audit forensics, not a sentinel
    marker (the lifecycle distinction lives in the audit row's
    ``method='DELETE'``). 404 on absent / cross-tenant -- the
    surface is deliberately NOT the idempotent-delete shape kb /
    memory use because the issue's "writes one history row + one
    audit_log row" requires a real row to delete from; an
    idempotent 204-on-missing would produce an audit row with no
    history counterpart.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_ID_DELETE,
        audit_op_class="write",
        audit_slug=slug,
    )
    try:
        await _service.delete_convention(
            session=session,
            operator=operator,
            slug=slug,
        )
    except ConventionNotFoundError as exc:
        raise _not_found() from exc
    # Conditional MCP resources/updated notification (T4 #316).
    # DELETE is also a resource-state change that subscribing
    # clients need to refresh on, per MCP 2025-06-18 §Resources.
    # No-op while ``resources.subscribe`` is False.
    _maybe_emit_resource_updated(tenant_id=operator.tenant_id, slug=slug)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# GET /api/v1/conventions/{slug}/history -- list history entries
# ---------------------------------------------------------------------------


@router.get("/{slug}/history", response_model=list[ConventionHistoryEntry])
async def list_history(
    operator: Annotated[Operator, _require_operator],
    session: Annotated[AsyncSession, Depends(get_session)],
    slug: Annotated[str, Path(max_length=_SLUG_MAX_LENGTH)],
) -> list[ConventionHistoryEntry]:
    """List history entries for one convention, newest first.

    Two-step lookup (resolve ``(tenant_id, slug)`` to convention
    id, then scan history) keeps the tenant-scope check explicit
    in the code; collapsing to a JOIN would not change cost profile
    but would obscure the boundary.

    Newest-first per the issue's "documented v0.2 ordering"
    acceptance. Cross-tenant 404 mirrors the show route.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_ID_HISTORY,
        audit_op_class="read",
        audit_slug=slug,
    )
    try:
        return await _service.list_history(
            session=session,
            tenant_id=operator.tenant_id,
            slug=slug,
        )
    except ConventionNotFoundError as exc:
        raise _not_found() from exc
