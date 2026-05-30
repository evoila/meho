# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/conventions*`` -- REST surface for tenant conventions.

Initiative #229 (G7.1), Task #314 (T2). Six routes mounted under
``/api/v1/conventions`` over the :class:`TenantConvention`
substrate (T1 #313). T3 (#315) layers CLI verbs over this
surface; T4 (#316) layers the session-preamble assembler that
reads through the same Pydantic shapes.

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
the handler pre-allocates the uuid via
:func:`~meho_backplane.audit.bind_preallocated_audit_id` and the
middleware honours it; G8's audit-query path joins on that
soft-FK for forensic queries.

Every route binds ``audit_op_id`` / ``audit_op_class`` /
``audit_slug`` contextvars before the DB probe so the audit row
classifies correctly even when the handler raises.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from fastapi.responses import Response
from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.audit import bind_preallocated_audit_id
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.conventions.preamble import (
    BLOCK_END as _CONVENTIONS_BLOCK_END,
)
from meho_backplane.conventions.preamble import (
    assemble_preamble,
    assemble_preamble_detailed,
)
from meho_backplane.conventions.schemas import (
    DEFAULT_MAX_PREAMBLE_TOKENS,
    BudgetStatus,
    Convention,
    ConventionCreate,
    ConventionHistoryEntry,
    ConventionKind,
    ConventionListResponse,
    ConventionSummary,
    ConventionUpdate,
    PreambleInclusion,
    estimate_tokens,
)
from meho_backplane.db.engine import get_session
from meho_backplane.db.models import TenantConvention, TenantConventionHistory
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


def _conventions_text_only(preamble_text: str) -> str:
    """Return the conventions text band from a combined preamble string.

    G12.4-T2 (#1316). The assembled preamble may now stitch two text
    bands: tenant conventions wrapped in
    ``<<TENANT_CONVENTIONS ... END_TENANT_CONVENTIONS>>`` followed by
    runbook priming wrapped in
    ``<<RUNBOOK_PRIMING — CRITICAL>> ... <<END_RUNBOOK_PRIMING>>``,
    separated by a blank line. The list endpoint's ``budget_status``
    reports the conventions-band token count only -- priming is
    bounded by its own implicit cap (``MAX_PRIMING_BLOCKS``) and is
    not charged to the conventions budget.

    Strategy: slice up to and including the conventions terminator
    (:data:`~meho_backplane.conventions.preamble.BLOCK_END`); whatever
    follows is the priming band (or empty). When the preamble carries
    only priming (no conventions), the terminator is absent and the
    function returns the empty string -- ``estimate_tokens("") == 0``,
    so the budget status correctly reports zero conventions weight
    for a tenant whose preamble is priming-only.

    The slice is a defensive read: it relies only on the wrapper-
    emitted terminator (never substituted from user content per the
    positional-wrapper discipline in
    :mod:`meho_backplane.conventions.preamble`), so a malicious
    convention body containing the literal terminator string cannot
    cause the slice to mis-attribute priming text as conventions
    text.
    """
    if not preamble_text:
        return ""
    end = preamble_text.find(_CONVENTIONS_BLOCK_END)
    if end == -1:
        # No conventions band in the assembled text -- it is
        # priming-only (operator has runs but tenant has no
        # operational conventions). The conventions-only weight is
        # zero.
        return ""
    return preamble_text[: end + len(_CONVENTIONS_BLOCK_END)]


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


def _enforce_budget(body: str, kind: ConventionKind) -> None:
    """Raise 422 when an ``operational`` body exceeds the preamble budget.

    The single-entry write-time gate the issue body calls out: a
    convention whose own token estimate exceeds
    :data:`DEFAULT_MAX_PREAMBLE_TOKENS` cannot fit the preamble at
    any priority, so failing the write loudly is the correct
    response (the alternative is silent drop at every future
    preamble assembly -- the failure mode the "``kubectl apply
    --dry-run=server`` discipline" is named for). The check is a
    no-op for ``workflow`` and ``reference`` conventions (which
    are not packed into the preamble).

    Detail message names ``estimated`` vs ``budget`` per the
    acceptance criterion's "detail names estimated vs budget"
    requirement; the integer values are useful for CLI users
    rewriting the body to fit.
    """
    if kind is not ConventionKind.OPERATIONAL:
        return
    estimated = estimate_tokens(body)
    if estimated > DEFAULT_MAX_PREAMBLE_TOKENS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"convention body exceeds preamble budget "
                f"(estimated={estimated}, budget={DEFAULT_MAX_PREAMBLE_TOKENS})"
            ),
        )


def _resolve_patch_fields(
    body: ConventionUpdate,
    existing: TenantConvention,
) -> tuple[str, str, int]:
    """Pick post-PATCH ``(title, body, priority)`` honouring v2 set-vs-null.

    Pydantic v2's :attr:`BaseModel.model_fields_set` distinguishes
    "field absent from JSON" from "field present with null". For
    each column in the PATCH surface: take the request body's
    value iff the field was both explicitly set AND non-null;
    otherwise fall through to the existing column. The type-
    narrowing yields concrete ``str`` / ``int`` locals so the
    handler's ORM assignments don't need ``cast`` calls.
    """
    explicit = body.model_fields_set
    title = body.title if "title" in explicit and body.title is not None else existing.title
    new_body = body.body if "body" in explicit and body.body is not None else existing.body
    priority = (
        body.priority if "priority" in explicit and body.priority is not None else existing.priority
    )
    return title, new_body, priority


def _enforce_patch_budget(
    body: ConventionUpdate,
    new_body: str,
    existing: TenantConvention,
) -> None:
    """Run the over-budget 422 gate iff the patch carries a body change.

    Priority-only or title-only PATCHes against an oversize body
    are intentionally not surfaced: the bug was already there at
    the prior write; rejecting on an unrelated edit would be
    confused-deputy behaviour. When ``body`` is in the patch, we
    validate against the existing kind (PATCH cannot change kind
    per :class:`ConventionUpdate`). An existing row carrying a
    kind value outside the closed :class:`ConventionKind`
    vocabulary (possible per T1's Out of scope on DB-level enum)
    falls back to :attr:`ConventionKind.REFERENCE` -- the safe
    direction; reference-kind never triggers the gate.
    """
    if "body" not in body.model_fields_set or body.body is None:
        return
    try:
        existing_kind = ConventionKind(existing.kind)
    except ValueError:
        existing_kind = ConventionKind.REFERENCE
    _enforce_budget(new_body, existing_kind)


async def _compute_preamble_status(
    *,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    operator_sub: str,
    slug: str,
    kind: ConventionKind,
) -> PreambleInclusion | None:
    """Resolve preamble inclusion for the just-written *(tenant, slug)* pair.

    G0.14-T8 (#1149, signal 18). Returns ``None`` for kinds that
    don't enter the preamble (``workflow`` / ``reference``); a
    populated :class:`PreambleInclusion` for ``operational`` rows.

    The function runs
    :func:`~meho_backplane.conventions.preamble.assemble_preamble_detailed`
    through the route handler's *session* so the pack reflects the
    in-progress write. SQLAlchemy 2.x reads within the same
    transaction see flushed-but-not-committed rows, so the caller
    must :meth:`session.flush` the convention INSERT/UPDATE before
    calling; the read here will then include it. Threading the
    route's session through (over opening a new one) means the
    feedback reflects the same state the post-commit MCP
    ``initialize`` handler will see -- no risk of an in-flight
    concurrent write from another request changing the answer
    mid-response.

    Three reads from the detailed assembly drive the four output
    fields:

    * ``kept_slugs.index(slug) + 1`` -> ``position`` (1-based, or
      ``None`` when this slug was dropped).
    * ``slug in kept_slugs`` -> ``included``.
    * ``token_counts[slug]`` -> ``token_count`` (own body weight).
    * ``dropped_slugs`` -> ``would_drop_slugs`` (verbatim).

    The ``slug`` argument is the slug as written (POST: ``body.slug``;
    PATCH: the path parameter — PATCH cannot rename, so the slug
    is stable across the update). ``kind`` is the convention's
    current kind: POST uses the request body's kind; PATCH uses the
    existing row's kind (PATCH cannot change kind per the
    :class:`ConventionUpdate` schema).

    G12.4-T2 (#1316) added the *operator_sub* parameter so the
    assembler can include runbook priming in the assembled preamble
    -- the post-write preview now reflects exactly what the operator's
    MCP session receives, priming included. The
    :class:`PreambleInclusion` projection (``position`` /
    ``included`` / ``token_count`` / ``would_drop_slugs``) is
    *conventions-only* -- those fields describe the conventions
    pack; the priming portion is summarised on the detailed
    assembly's ``runbook_block_count`` / ``runbook_summarized``
    fields, which are not surfaced here (the route's
    :class:`PreambleInclusion` schema is the slug-scoped feedback
    surface, not a general preamble report).
    """
    if kind is not ConventionKind.OPERATIONAL:
        # ``workflow`` / ``reference`` are not preamble-bound -- a
        # write against them does not affect the assembled preamble,
        # so the operator's "did this land in the preamble?" question
        # doesn't apply. Returning ``None`` (over a stub
        # ``PreambleInclusion(included=False, ...)``) keeps the
        # response shape honest: ``preamble_status`` present iff the
        # write was a preamble-bound one.
        return None
    assembly = await assemble_preamble_detailed(
        tenant_id,
        operator_sub,
        session=session,
    )
    included = slug in assembly.kept_slugs
    position = assembly.kept_slugs.index(slug) + 1 if included else None
    # ``token_counts`` carries every considered slug (kept + dropped);
    # the just-written slug must appear there because the assembler
    # ran after the write flushed. The ``get`` fallback is paranoia
    # against an unforeseen race (e.g. a concurrent DELETE) and
    # short-circuits to 0 so the response shape still validates.
    token_count = assembly.token_counts.get(slug, 0)
    return PreambleInclusion(
        included=included,
        position=position,
        token_count=token_count,
        would_drop_slugs=assembly.dropped_slugs,
    )


async def _load_convention(
    *,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    slug: str,
) -> TenantConvention | None:
    """Fetch the convention by ``(tenant_id, slug)`` or return ``None``.

    Hits the composite-unique index
    ``tenant_conventions_tenant_slug_idx`` (per the substrate's
    declared index discipline) -- one btree probe. ``None`` means
    "the row does not exist in this operator's tenant"; collapses
    "wrong tenant" and "wrong slug" into the same return value so
    the route layer can apply the consistent 404 the tenant
    boundary's info-leak avoidance requires.
    """
    stmt = select(TenantConvention).where(
        TenantConvention.tenant_id == tenant_id,
        TenantConvention.slug == slug,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


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

    The response also carries ``budget_status`` (T7 #1094): the
    preamble budget arithmetic for the operator's tenant, computed
    by a call to :func:`~meho_backplane.conventions.preamble.assemble_preamble`.
    The ``--kind`` query filter narrows the ``entries`` list only;
    ``budget_status`` always reflects the full ``operational``
    set (a ``--kind=workflow`` list call still wants the truthful
    budget signal). The assembler runs an indexed SELECT + an
    in-memory pack -- O(N) per call where N is the tenant's
    operational-convention count (~10-20 in practice), so the
    cost is negligible against the list round-trip.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_ID_LIST,
        audit_op_class="read",
    )
    stmt = select(TenantConvention).where(
        TenantConvention.tenant_id == operator.tenant_id,
    )
    if kind is not None:
        stmt = stmt.where(TenantConvention.kind == kind.value)
    stmt = stmt.order_by(
        TenantConvention.priority.desc(),
        TenantConvention.created_at.asc(),
    )
    rows = (await session.execute(stmt)).scalars().all()

    # Compute the budget status via the same primitive T4's MCP
    # ``initialize`` handler uses, so a tenant's
    # ``estimated_tokens`` on the list response and the preamble
    # actually delivered to agent sessions cannot drift. The
    # assembler opens its own DB session (it has to: the MCP
    # ``initialize`` path is not a FastAPI request handler), which
    # adds one round-trip on top of the list query above; that is
    # acceptable for the v0.2 budget contract and the natural unit
    # for "what the preamble actually weighs".
    #
    # G12.4-T2 (#1316) extended :func:`assemble_preamble` with the
    # operator's ``sub`` so the assembled text can include per-run
    # runbook priming. The ``budget_status`` arithmetic is
    # **conventions-only**: ``estimated_tokens`` measures the
    # conventions text band against ``DEFAULT_MAX_PREAMBLE_TOKENS``
    # (priming has its own implicit cap via ``MAX_PRIMING_BLOCKS``
    # and is not charged to the conventions budget per the
    # Initiative #1199 design contract). The operator's preamble is
    # still assembled with priming so the list view reflects the
    # exact wire shape the MCP ``initialize`` handler ships; the
    # priming portion is then stripped before measuring tokens so
    # the budget signal stays honest -- a tenant with zero
    # operational conventions reports ``estimated_tokens=0``
    # regardless of how many runbook runs the operator has.
    preamble = await assemble_preamble(operator.tenant_id, operator.sub)
    conventions_only_text = _conventions_text_only(preamble.text)
    budget_status = BudgetStatus(
        max_tokens=DEFAULT_MAX_PREAMBLE_TOKENS,
        estimated_tokens=estimate_tokens(conventions_only_text),
        over_budget=bool(preamble.dropped_slugs),
        dropped_slugs=preamble.dropped_slugs,
    )

    return ConventionListResponse(
        entries=[ConventionSummary.model_validate(row) for row in rows],
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
    row = await _load_convention(
        session=session,
        tenant_id=operator.tenant_id,
        slug=slug,
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="convention_not_found",
        )
    return Convention.model_validate(row)


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
    _enforce_budget(body.body, body.kind)

    audit_id = uuid.uuid4()
    bind_preallocated_audit_id(audit_id)
    now = datetime.now(UTC)
    convention = TenantConvention(
        id=uuid.uuid4(),
        tenant_id=operator.tenant_id,
        slug=body.slug,
        title=body.title,
        body=body.body,
        kind=body.kind.value,
        priority=body.priority,
        created_by_sub=operator.sub,
        created_at=now,
        updated_at=now,
    )
    history = TenantConventionHistory(
        id=uuid.uuid4(),
        convention_id=convention.id,
        body_before=None,
        body_after=body.body,
        actor_sub=operator.sub,
        ts=now,
        audit_id=audit_id,
    )
    session.add_all([convention, history])
    try:
        await session.flush()
    except IntegrityError as exc:
        # Narrow the 409 to actual composite-unique-index violations
        # on ``(tenant_id, slug)``; other IntegrityError shapes
        # (CHECK / NOT NULL violations from a future tightening
        # migration) propagate so a genuine corruption surfaces as
        # a 500 rather than a misleading "already exists".
        #
        # PG via asyncpg: ``PostgresError.sqlstate == "23505"`` is
        # the unique-violation code. SQLite: ``UNIQUE constraint
        # failed`` substring is the documented form. Same shape the
        # broadcast_overrides router uses for the same purpose.
        orig = getattr(exc, "orig", None)
        sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
        orig_msg = str(orig or exc)
        is_unique_violation = sqlstate == "23505" or "UNIQUE constraint failed" in orig_msg
        if is_unique_violation:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"convention {body.slug!r} already exists",
            ) from exc
        raise
    # Refresh so any DB-side defaults are visible on the returned
    # row (mirrors the broadcast_overrides + memory surfaces).
    await session.refresh(convention)
    # G0.14-T8 (#1149, signal 18) preamble-status feedback.
    # The convention has flushed inside the route's session (above
    # at ``session.flush()``); reads through the same session see it,
    # so the preamble assembler resolves the just-written slug's
    # position against the post-write state. ``None`` for
    # workflow/reference (preamble-unbound kinds).
    preamble_status = await _compute_preamble_status(
        session=session,
        tenant_id=operator.tenant_id,
        operator_sub=operator.sub,
        slug=body.slug,
        kind=body.kind,
    )
    # Conditional MCP resources/updated notification (T4 #316).
    # No-op while ``resources.subscribe`` is False; structured emit
    # call site is in place for the v0.2.next subscribe flip.
    _maybe_emit_resource_updated(tenant_id=operator.tenant_id, slug=body.slug)
    return Convention.model_validate(convention).model_copy(
        update={"preamble_status": preamble_status},
    )


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

    existing = await _load_convention(
        session=session,
        tenant_id=operator.tenant_id,
        slug=slug,
    )
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="convention_not_found",
        )

    new_title, new_body, new_priority = _resolve_patch_fields(body, existing)
    _enforce_patch_budget(body, new_body, existing)

    audit_id = uuid.uuid4()
    bind_preallocated_audit_id(audit_id)
    now = datetime.now(UTC)
    body_before = existing.body

    existing.title = new_title
    existing.body = new_body
    existing.priority = new_priority
    existing.updated_at = now

    history = TenantConventionHistory(
        id=uuid.uuid4(),
        convention_id=existing.id,
        body_before=body_before,
        body_after=new_body,
        actor_sub=operator.sub,
        ts=now,
        audit_id=audit_id,
    )
    session.add(history)
    await session.flush()
    await session.refresh(existing)
    # G0.14-T8 (#1149, signal 18) preamble-status feedback. PATCH
    # cannot change kind per :class:`ConventionUpdate`, so the
    # post-PATCH kind is the existing row's kind; resolve back to
    # the enum for the helper. An existing row carrying a kind
    # outside the closed :class:`ConventionKind` vocabulary
    # (possible per T1's Out of scope on DB-level enum) falls back
    # to ``REFERENCE`` -- the safe direction; reference-kind
    # short-circuits to ``preamble_status=None`` (preamble-unbound).
    try:
        existing_kind = ConventionKind(existing.kind)
    except ValueError:
        existing_kind = ConventionKind.REFERENCE
    preamble_status = await _compute_preamble_status(
        session=session,
        tenant_id=operator.tenant_id,
        operator_sub=operator.sub,
        slug=slug,
        kind=existing_kind,
    )
    # Conditional MCP resources/updated notification (T4 #316).
    # No-op while ``resources.subscribe`` is False; structured emit
    # call site is in place for the v0.2.next subscribe flip.
    _maybe_emit_resource_updated(tenant_id=operator.tenant_id, slug=slug)
    return Convention.model_validate(existing).model_copy(
        update={"preamble_status": preamble_status},
    )


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

    existing = await _load_convention(
        session=session,
        tenant_id=operator.tenant_id,
        slug=slug,
    )
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="convention_not_found",
        )

    audit_id = uuid.uuid4()
    bind_preallocated_audit_id(audit_id)
    now = datetime.now(UTC)
    convention_id = existing.id

    history = TenantConventionHistory(
        id=uuid.uuid4(),
        convention_id=convention_id,
        body_before=existing.body,
        body_after=existing.body,
        actor_sub=operator.sub,
        ts=now,
        audit_id=audit_id,
    )
    session.add(history)
    # Flush the history row before the DELETE -- SQLAlchemy 2.x's
    # session ordering between pending inserts and deletes is
    # implementation-defined, and the explicit flush keeps the
    # row-pair contract T4/T5 expect.
    await session.flush()
    await session.execute(
        sql_delete(TenantConvention).where(
            TenantConvention.id == convention_id,
            TenantConvention.tenant_id == operator.tenant_id,
        ),
    )
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

    existing = await _load_convention(
        session=session,
        tenant_id=operator.tenant_id,
        slug=slug,
    )
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="convention_not_found",
        )

    stmt = (
        select(TenantConventionHistory)
        .where(TenantConventionHistory.convention_id == existing.id)
        .order_by(TenantConventionHistory.ts.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [ConventionHistoryEntry.model_validate(row) for row in rows]
