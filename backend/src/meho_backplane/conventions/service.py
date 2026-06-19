# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``ConventionsService`` -- session-threaded CRUD over tenant conventions.

G10.12-T0 (#1894). Lifts the budget gate, history-row + pre-allocated
audit-id pairing, and the preamble-feedback logic that previously
lived inline in :mod:`meho_backplane.api.v1.conventions` into a thin
service both the REST surface and the in-process console BFF (T1/T2
#1838) can call without re-deriving the arithmetic.

Session threading
-----------------

Unlike :class:`~meho_backplane.memory.service.MemoryService` (which
opens its own session via the sessionmaker), every method here takes
an explicit ``session: AsyncSession``. Two consumers need different
session provenance:

* the REST handler threads its request-scoped
  :func:`~meho_backplane.db.engine.get_session` dependency, so the
  audit middleware's pre-allocated-id soft-FK and the post-write
  read-your-own-writes preamble preview share one transaction;
* the cookie-authed BFF UI opens its own in-process session and
  threads it the same way.

The pre-allocated-audit-id seam (``bind_preallocated_audit_id``) is
therefore exercised identically by both callers: the service mints
the uuid, binds it onto the structlog contextvar the
:class:`~meho_backplane.audit.AuditMiddleware` reads, and writes the
paired ``tenant_convention_history`` row carrying that same id in the
same transaction.

Error vocabulary
----------------

HTTP concerns stay on the route handler. The service raises a small
typed-error vocabulary the consumer maps to its own transport:

* :class:`ConventionNotFoundError` -- the ``(tenant_id, slug)`` pair has no
  row (route: 404; BFF: HTMX 404 partial). Collapses "wrong tenant"
  and "wrong slug" into one error so the tenant-boundary info-leak
  avoidance contract is preserved by construction.
* :class:`ConventionConflictError` -- a create hit the composite-unique
  index on ``(tenant_id, slug)`` (route: 409).
* :class:`OverBudgetError` -- an ``operational`` write whose own token
  estimate exceeds :data:`DEFAULT_MAX_PREAMBLE_TOKENS` (route: 422).
  Carries ``estimated`` / ``budget`` ints so the consumer renders the
  same actionable detail the inline gate did.

``audit_op_id`` / ``audit_op_class`` / ``audit_slug`` contextvar
binding stays on the route handler -- the audit classification differs
per HTTP route, which is a transport concern, not a service one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.audit import bind_preallocated_audit_id
from meho_backplane.auth.operator import Operator
from meho_backplane.conventions._internal import (
    ConventionConflictError,
    ConventionNotFoundError,
    ConventionServiceError,
    OverBudgetError,
    conventions_text_only,
    enforce_budget,
    enforce_patch_budget,
    resolve_patch_fields,
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
    ConventionSummary,
    ConventionUpdate,
    PreambleInclusion,
    estimate_tokens,
)
from meho_backplane.db.models import TenantConvention, TenantConventionHistory

__all__ = [
    "ConventionConflictError",
    "ConventionNotFoundError",
    "ConventionServiceError",
    "ConventionsService",
    "OverBudgetError",
]


class ConventionsService:
    """Session-threaded CRUD + budget + history service for tenant conventions.

    Plain class (mirrors :class:`MemoryService`'s shape) with no held
    state -- every method takes the ``session`` to operate on, so both
    the REST request-scoped session and a UI in-process session thread
    through the same code path. The pre-allocated-audit-id seam, the
    history-row pairing, and the budget arithmetic live here once; the
    consumers never re-derive them.
    """

    async def _load_convention(
        self,
        *,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        slug: str,
    ) -> TenantConvention | None:
        """Fetch the convention by ``(tenant_id, slug)`` or return ``None``.

        Hits the composite-unique index -- one btree probe. ``None``
        means "the row does not exist in this operator's tenant";
        collapses "wrong tenant" and "wrong slug" into the same return
        value so the consumer applies the consistent 404 the tenant
        boundary's info-leak avoidance requires.
        """
        stmt = select(TenantConvention).where(
            TenantConvention.tenant_id == tenant_id,
            TenantConvention.slug == slug,
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _compute_preamble_status(
        self,
        *,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        operator_sub: str,
        slug: str,
        kind: ConventionKind,
    ) -> PreambleInclusion | None:
        """Resolve preamble inclusion for the just-written *(tenant, slug)* pair.

        Returns ``None`` for kinds that don't enter the preamble
        (``workflow`` / ``reference``); a populated
        :class:`PreambleInclusion` for ``operational`` rows.

        Runs :func:`assemble_preamble_detailed` through the caller's
        *session* so the pack reflects the in-progress write.
        SQLAlchemy 2.x reads within the same transaction see
        flushed-but-not-committed rows, so the caller must flush the
        convention INSERT/UPDATE before calling; the read here then
        includes it.
        """
        if kind is not ConventionKind.OPERATIONAL:
            return None
        assembly = await assemble_preamble_detailed(
            tenant_id,
            operator_sub,
            session=session,
        )
        included = slug in assembly.kept_slugs
        position = assembly.kept_slugs.index(slug) + 1 if included else None
        token_count = assembly.token_counts.get(slug, 0)
        return PreambleInclusion(
            included=included,
            position=position,
            token_count=token_count,
            would_drop_slugs=assembly.dropped_slugs,
        )

    async def budget_status(
        self,
        *,
        session: AsyncSession,
        operator: Operator,
    ) -> BudgetStatus:
        """Compute the conventions preamble budget for *operator*'s tenant.

        Runs the same :func:`assemble_preamble` primitive T4's MCP
        ``initialize`` handler uses, so ``estimated_tokens`` and the
        preamble actually delivered to agent sessions cannot drift. The
        assembler opens its own DB session (the *session* argument is
        accepted for signature parity with the other methods but the
        assembler does not thread it -- the budget read is a
        committed-state snapshot, not a read-your-own-writes preview).

        The arithmetic is **conventions-only**: the preamble is
        assembled with priming + catalogue bands (so the list view
        reflects the exact wire shape MCP ``initialize`` ships), then
        ``conventions_text_only`` strips everything after the
        conventions terminator before measuring.
        """
        preamble = await assemble_preamble(
            operator.tenant_id, operator.sub, capabilities=operator.capabilities
        )
        conventions_only_text = conventions_text_only(preamble.text)
        return BudgetStatus(
            max_tokens=DEFAULT_MAX_PREAMBLE_TOKENS,
            estimated_tokens=estimate_tokens(conventions_only_text),
            over_budget=bool(preamble.dropped_slugs),
            dropped_slugs=preamble.dropped_slugs,
        )

    async def list_conventions(
        self,
        *,
        session: AsyncSession,
        operator: Operator,
        kind: ConventionKind | None = None,
    ) -> tuple[list[ConventionSummary], BudgetStatus]:
        """List the operator's tenant's conventions + the tenant budget.

        Returns the lighter :class:`ConventionSummary` shape (no full
        ``body``). Ordering is ``priority DESC, created_at ASC`` -- the
        same key the preamble assembler uses for packing. The ``kind``
        filter narrows the returned entries only; ``budget_status``
        always reflects the full ``operational`` set (a
        ``kind=workflow`` list still wants the truthful budget signal).
        """
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
        entries = [ConventionSummary.model_validate(row) for row in rows]
        budget = await self.budget_status(session=session, operator=operator)
        return entries, budget

    async def get_convention(
        self,
        *,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        slug: str,
    ) -> Convention:
        """Fetch one convention by slug.

        Raises :class:`ConventionNotFoundError` on absent OR cross-tenant
        (the lookup filters on both ``tenant_id`` AND ``slug``), which
        the route maps to 404 -- preserving the tenant-boundary
        info-leak avoidance contract.
        """
        row = await self._load_convention(
            session=session,
            tenant_id=tenant_id,
            slug=slug,
        )
        if row is None:
            raise ConventionNotFoundError(slug)
        return Convention.model_validate(row)

    async def create_convention(
        self,
        *,
        session: AsyncSession,
        operator: Operator,
        body: ConventionCreate,
    ) -> Convention:
        """Create one convention; writes one history row in the same transaction.

        Sequence: budget gate -> pre-allocate audit_id -> insert
        convention + history rows -> conflict mapping on duplicate
        ``(tenant_id, slug)`` -> preamble-status feedback. The CREATE
        history row has ``body_before=NULL``; ``body_after=body.body``,
        and carries the pre-allocated ``audit_id`` soft-FK that the
        audit middleware honours for the matching audit row.

        Raises :class:`OverBudgetError` (route: 422) and
        :class:`ConventionConflictError` (route: 409).
        """
        enforce_budget(body.body, body.kind)

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
            # Narrow the conflict to actual composite-unique-index
            # violations on ``(tenant_id, slug)``; other IntegrityError
            # shapes (CHECK / NOT NULL from a future tightening
            # migration) propagate so a genuine corruption surfaces as
            # a 500 rather than a misleading "already exists".
            #
            # PG via asyncpg: ``sqlstate == "23505"`` is the
            # unique-violation code. SQLite: ``UNIQUE constraint
            # failed`` substring is the documented form.
            orig = getattr(exc, "orig", None)
            sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
            orig_msg = str(orig or exc)
            is_unique_violation = sqlstate == "23505" or "UNIQUE constraint failed" in orig_msg
            if is_unique_violation:
                raise ConventionConflictError(body.slug) from exc
            raise
        await session.refresh(convention)
        preamble_status = await self._compute_preamble_status(
            session=session,
            tenant_id=operator.tenant_id,
            operator_sub=operator.sub,
            slug=body.slug,
            kind=body.kind,
        )
        return Convention.model_validate(convention).model_copy(
            update={"preamble_status": preamble_status},
        )

    async def update_convention(
        self,
        *,
        session: AsyncSession,
        operator: Operator,
        slug: str,
        body: ConventionUpdate,
    ) -> Convention:
        """Update one convention; writes one history row in the same transaction.

        Applies only fields explicitly set in the request body (the v2
        ``model_fields_set`` set-vs-null pattern). The budget gate fires
        on a ``body`` change; PATCH cannot change ``kind`` so the check
        uses the existing row's kind. A priority-only or title-only
        PATCH still writes a history row carrying the unchanged body in
        both ``body_before`` and ``body_after``.

        Raises :class:`ConventionNotFoundError` (route: 404) and
        :class:`OverBudgetError` (route: 422).
        """
        existing = await self._load_convention(
            session=session,
            tenant_id=operator.tenant_id,
            slug=slug,
        )
        if existing is None:
            raise ConventionNotFoundError(slug)

        new_title, new_body, new_priority = resolve_patch_fields(body, existing)
        enforce_patch_budget(body, new_body, existing)

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
        # PATCH cannot change kind, so the post-PATCH kind is the
        # existing row's kind; resolve back to the enum. A row carrying
        # a kind outside the closed vocabulary falls back to
        # ``REFERENCE`` (the safe direction; reference-kind
        # short-circuits to ``preamble_status=None``).
        try:
            existing_kind = ConventionKind(existing.kind)
        except ValueError:
            existing_kind = ConventionKind.REFERENCE
        preamble_status = await self._compute_preamble_status(
            session=session,
            tenant_id=operator.tenant_id,
            operator_sub=operator.sub,
            slug=slug,
            kind=existing_kind,
        )
        return Convention.model_validate(existing).model_copy(
            update={"preamble_status": preamble_status},
        )

    async def delete_convention(
        self,
        *,
        session: AsyncSession,
        operator: Operator,
        slug: str,
    ) -> None:
        """Delete one convention; writes one history row in the same transaction.

        **Non-idempotent**: raises :class:`ConventionNotFoundError` (route:
        404) on a missing / cross-tenant slug rather than a silent
        no-op -- the history + audit row pairing requires a real row to
        delete from; an idempotent 204-on-missing would produce an
        audit row with no history counterpart. The history row carries
        ``body_after=<final body>`` -- a legible last-known state for
        forensics.
        """
        existing = await self._load_convention(
            session=session,
            tenant_id=operator.tenant_id,
            slug=slug,
        )
        if existing is None:
            raise ConventionNotFoundError(slug)

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
        # ordering between pending inserts and deletes is
        # implementation-defined; the explicit flush keeps the row-pair
        # contract.
        await session.flush()
        await session.execute(
            sql_delete(TenantConvention).where(
                TenantConvention.id == convention_id,
                TenantConvention.tenant_id == operator.tenant_id,
            ),
        )

    async def list_history(
        self,
        *,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        slug: str,
    ) -> list[ConventionHistoryEntry]:
        """List history entries for one convention, newest first.

        Two-step lookup (resolve ``(tenant_id, slug)`` to convention id,
        then scan history) keeps the tenant-scope check explicit.
        Raises :class:`ConventionNotFoundError` (route: 404) on absent /
        cross-tenant.
        """
        existing = await self._load_convention(
            session=session,
            tenant_id=tenant_id,
            slug=slug,
        )
        if existing is None:
            raise ConventionNotFoundError(slug)

        stmt = (
            select(TenantConventionHistory)
            .where(TenantConventionHistory.convention_id == existing.id)
            .order_by(TenantConventionHistory.ts.desc())
        )
        rows = (await session.execute(stmt)).scalars().all()
        return [ConventionHistoryEntry.model_validate(row) for row in rows]
