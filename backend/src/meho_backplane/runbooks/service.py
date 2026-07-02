# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tenant-scoped :class:`RunbookTemplateService` over the G12.1 storage substrate.

Initiative #1197 (G12.2) T2 surface. The REST routes (T3) and MCP tools
(T4) wrap this service rather than touching
:mod:`meho_backplane.db.models` directly, so the versioning algebra
(edit-draft vs fork-from-published), the status state machine
(``draft -> published -> deprecated``), and the ``in_flight_run_count``
query all live in one place.

Concurrency model
-----------------

:class:`RunbookTemplateService` is stateless and method-scoped: each
public method opens its own :class:`AsyncSession` via
:func:`~meho_backplane.db.engine.get_sessionmaker` and commits
synchronously before returning. This mirrors
:class:`meho_backplane.kb.service.KbService` -- the service is trivially
instantiable (no constructor parameters) and never shares transaction
state across calls.

Tenant scoping
--------------

Every public method takes ``tenant_id`` as the first parameter -- no
contextvar resolution. The route / MCP layers (T3 / T4) bind the value
from the operator's JWT; the service is testable in isolation and the
tenant boundary is auditable at the call site. Every query filters on
``tenant_id`` so cross-tenant reads are structurally impossible.

RBAC
----

This service does **not** enforce roles. A senior-vs-operator gate on
who may draft / publish / deprecate is the route / MCP boundary's job
(T3 / T4). The service is willing to read or write any row inside the
tenant it was handed.

Versioning algebra
------------------

The load-bearing decision is in :meth:`RunbookTemplateService.update_or_fork`:
the caller does not pick the path. If a draft exists for the slug, the
edit mutates it in place (version unchanged). If only published /
deprecated versions exist, the edit forks a new draft at
``max(version) + 1`` and reports the source it forked from -- including
how many runs are still pinned to that source version
(:attr:`~meho_backplane.runbooks.schemas.ForkInfo.in_flight_run_count`).
That count is the **only** read of ``runbook_runs`` from the template
side; G12.3's run service is the durable owner of that table.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import RunbookRun, RunbookTemplate
from meho_backplane.kb.schemas import validate_slug
from meho_backplane.runbooks.schemas import (
    DeprecateTemplateRequest,
    DeprecateTemplateResponse,
    DiscardTemplateRequest,
    DiscardTemplateResponse,
    DraftTemplateRequest,
    DraftTemplateResponse,
    EditTemplateRequest,
    EditTemplateResponse,
    ForkInfo,
    ListTemplatesFilter,
    PublishTemplateRequest,
    PublishTemplateResponse,
    RunbookTemplateBody,
    ShowTemplateResponse,
    Step,
    TemplateSummary,
)

__all__ = [
    "DeprecatedTemplateError",
    "DuplicateDraftError",
    "RunbookTemplateService",
    "TemplateNotDraftError",
    "TemplateNotFoundError",
    "TemplateNotPublishedError",
]


#: The closed status vocabulary, mirroring the ``CheckConstraint`` on
#: :class:`~meho_backplane.db.models.RunbookTemplate`. Used by
#: :func:`_narrow_status` to coerce the ``str`` column into the
#: ``Literal``-typed response fields.
_TemplateStatus = Literal["draft", "published", "deprecated"]
_TEMPLATE_STATUSES: frozenset[str] = frozenset({"draft", "published", "deprecated"})

#: The run state that counts toward :class:`ForkInfo.in_flight_run_count`.
#: Matches the ``runbook_runs.state`` vocabulary on
#: :class:`~meho_backplane.db.models.RunbookRun` (``in_progress`` /
#: ``completed`` / ``abandoned``); only ``in_progress`` runs are pinned to
#: a version the senior cares about when deciding whether to fork.
_IN_PROGRESS_STATE: str = "in_progress"


class TemplateNotFoundError(LookupError):
    """Raised when ``(tenant_id, slug, version?)`` doesn't resolve to a row."""


class TemplateNotDraftError(ValueError):
    """Raised when a draft-only op hits a non-draft template.

    Both ``publish`` (a published/deprecated version cannot be re-published)
    and ``discard`` (a published/deprecated version is retired via
    ``deprecate``, never discarded) require the target to be a draft.
    """


class TemplateNotPublishedError(ValueError):
    """Raised when deprecate is called against a non-published template."""


class DuplicateDraftError(ValueError):
    """Raised when ``create_draft`` finds an existing draft for the same slug."""


class DeprecatedTemplateError(ValueError):
    """Raised by ``start_run`` (G12.3) against a deprecated version.

    Defined here so the runbook service module owns the full error
    vocabulary even though the consumer is the G12.3 run service.
    """


class RunbookTemplateService:
    """Tenant-scoped CRUD + versioning over runbook templates.

    Stateless and async; instantiate once and call freely. Each public
    method opens its own DB session, commits, and closes -- no shared
    transaction state across calls. The class ships with no constructor
    parameters: every dependency (the engine) is bound via the
    module-level singletons the G0.4 substrate set up.
    """

    async def create_draft(
        self,
        tenant_id: uuid.UUID,
        operator_sub: str,
        request: DraftTemplateRequest,
    ) -> DraftTemplateResponse:
        """Insert a new draft for *request.slug*.

        ``version=1`` when no row exists for the slug; otherwise raises
        :class:`DuplicateDraftError` -- the slug already has a version
        (a draft, or a published/deprecated history), so the caller wants
        :meth:`update_or_fork`, not a second v1. The slug is revalidated
        against
        :data:`~meho_backplane.kb.schemas.SLUG_PATTERN` at the service
        boundary -- defense in depth even though the request model
        already enforced it.
        """
        validate_slug(request.slug)

        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            latest = await _resolve_latest_version(session, tenant_id, request.slug)
            if latest is not None:
                raise DuplicateDraftError(
                    f"slug {request.slug!r} already has a version "
                    f"(latest={latest}); use edit to fork or mutate the draft"
                )

            now = datetime.now(UTC)
            row = RunbookTemplate(
                tenant_id=tenant_id,
                slug=request.slug,
                version=1,
                title=request.body.title,
                description=request.body.description,
                target_kind=request.body.target_kind,
                steps=_steps_to_storage(request.body.steps),
                status="draft",
                created_by=operator_sub,
                created_at=now,
                edited_by=operator_sub,
                edited_at=now,
            )
            session.add(row)
            await session.commit()

        structlog.get_logger().info(
            "runbook_draft_created",
            tenant_id=str(tenant_id),
            slug=request.slug,
            version=1,
        )
        return DraftTemplateResponse(slug=request.slug, version=1, status="draft")

    async def update_or_fork(
        self,
        tenant_id: uuid.UUID,
        operator_sub: str,
        request: EditTemplateRequest,
    ) -> EditTemplateResponse:
        """Edit a draft in place, or fork a new draft from the latest published.

        Two paths, picked by the service (the caller does not choose):

        1. A draft exists for the slug -> mutate it in place. The version
           is unchanged, ``edited_by`` / ``edited_at`` advance, and
           :attr:`EditTemplateResponse.forked_from` is ``None``.
        2. Only published / deprecated versions exist -> fork a new draft
           at ``max(version) + 1`` (``status='draft'``).
           :attr:`EditTemplateResponse.forked_from` carries the source
           version's slug + version + ``in_flight_run_count``.

        Raises :class:`TemplateNotFoundError` when the slug has no rows at
        all (nothing to edit or fork from).
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            draft = await self._load_draft(session, tenant_id, request.slug)
            now = datetime.now(UTC)

            if draft is not None:
                # Path 1: in-place draft mutation. Version stays.
                draft.title = request.body.title
                draft.description = request.body.description
                draft.target_kind = request.body.target_kind
                draft.steps = _steps_to_storage(request.body.steps)
                draft.edited_by = operator_sub
                draft.edited_at = now
                version = draft.version
                await session.commit()
                forked_from: ForkInfo | None = None
            else:
                # Path 2: fork from the latest version. No draft exists,
                # so the latest is a published or deprecated row.
                source_version = await _resolve_latest_version(session, tenant_id, request.slug)
                if source_version is None:
                    raise TemplateNotFoundError(
                        f"no template for slug {request.slug!r}; nothing to edit or fork"
                    )
                in_flight = await _count_in_flight_runs(
                    session, tenant_id, request.slug, source_version
                )
                version = source_version + 1
                row = RunbookTemplate(
                    tenant_id=tenant_id,
                    slug=request.slug,
                    version=version,
                    title=request.body.title,
                    description=request.body.description,
                    target_kind=request.body.target_kind,
                    steps=_steps_to_storage(request.body.steps),
                    status="draft",
                    created_by=operator_sub,
                    created_at=now,
                    edited_by=operator_sub,
                    edited_at=now,
                )
                session.add(row)
                await session.commit()
                forked_from = ForkInfo(
                    slug=request.slug,
                    version=source_version,
                    in_flight_run_count=in_flight,
                )

        structlog.get_logger().info(
            "runbook_template_edited",
            tenant_id=str(tenant_id),
            slug=request.slug,
            version=version,
            forked=forked_from is not None,
        )
        return EditTemplateResponse(
            slug=request.slug,
            version=version,
            status="draft",
            forked_from=forked_from,
        )

    async def publish(
        self,
        tenant_id: uuid.UUID,
        request: PublishTemplateRequest,
    ) -> PublishTemplateResponse:
        """Promote ``(tenant_id, slug, version)`` from draft to published.

        Idempotent: re-publishing an already-published version is a no-op
        and returns the same response. Raises
        :class:`TemplateNotFoundError` when the row doesn't exist and
        :class:`TemplateNotDraftError` when it exists but is deprecated.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            row = await self._load_exact(session, tenant_id, request.slug, request.version)
            if row is None:
                raise TemplateNotFoundError(
                    f"no template {request.slug!r} v{request.version} for tenant"
                )
            if row.status == "published":
                # Idempotent no-op -- already in the target state.
                return PublishTemplateResponse(
                    slug=request.slug, version=request.version, status="published"
                )
            if row.status != "draft":
                raise TemplateNotDraftError(
                    f"template {request.slug!r} v{request.version} is "
                    f"{row.status!r}, not draft; cannot publish"
                )
            row.status = "published"
            await session.commit()

        structlog.get_logger().info(
            "runbook_template_published",
            tenant_id=str(tenant_id),
            slug=request.slug,
            version=request.version,
        )
        return PublishTemplateResponse(
            slug=request.slug, version=request.version, status="published"
        )

    async def deprecate(
        self,
        tenant_id: uuid.UUID,
        request: DeprecateTemplateRequest,
    ) -> DeprecateTemplateResponse:
        """Retire ``(tenant_id, slug, version)`` from published to deprecated.

        Idempotent: re-deprecating an already-deprecated version is a
        no-op and returns the same response. Raises
        :class:`TemplateNotFoundError` when the row doesn't exist and
        :class:`TemplateNotPublishedError` when it exists but is still a
        draft.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            row = await self._load_exact(session, tenant_id, request.slug, request.version)
            if row is None:
                raise TemplateNotFoundError(
                    f"no template {request.slug!r} v{request.version} for tenant"
                )
            if row.status == "deprecated":
                # Idempotent no-op -- already in the target state.
                return DeprecateTemplateResponse(
                    slug=request.slug, version=request.version, status="deprecated"
                )
            if row.status != "published":
                raise TemplateNotPublishedError(
                    f"template {request.slug!r} v{request.version} is "
                    f"{row.status!r}, not published; cannot deprecate"
                )
            row.status = "deprecated"
            await session.commit()

        structlog.get_logger().info(
            "runbook_template_deprecated",
            tenant_id=str(tenant_id),
            slug=request.slug,
            version=request.version,
        )
        return DeprecateTemplateResponse(
            slug=request.slug, version=request.version, status="deprecated"
        )

    async def discard(
        self,
        tenant_id: uuid.UUID,
        request: DiscardTemplateRequest,
    ) -> DiscardTemplateResponse:
        """Delete an **unpublished draft** ``(tenant_id, slug, version)``.

        Completes the template CRUD lifecycle with a delete-for-drafts leg:
        an operator who drafts incorrectly (typo'd slug, wrong steps) can
        remove the draft cleanly instead of the publish-then-deprecate
        workaround. Only ``draft`` rows are discardable -- a ``published``
        or ``deprecated`` version raises :class:`TemplateNotDraftError`
        (those are retired via :meth:`deprecate`, never erased, preserving
        the audit/lifecycle of anything that was ever live). A missing
        ``(tenant, slug, version)`` triple raises
        :class:`TemplateNotFoundError` -- the same not-found posture as
        :meth:`publish` / :meth:`deprecate` (so a re-discard of an
        already-removed draft is a 404, not a silent success).

        A pure draft cannot have runs (``start_run`` pins to a *published*
        version), so there is no cascade to handle -- the row is deleted
        outright.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            row = await self._load_exact(session, tenant_id, request.slug, request.version)
            if row is None:
                raise TemplateNotFoundError(
                    f"no template {request.slug!r} v{request.version} for tenant"
                )
            if row.status != "draft":
                raise TemplateNotDraftError(
                    f"template {request.slug!r} v{request.version} is "
                    f"{row.status!r}, not draft; cannot discard "
                    f"(use deprecate to retire a published version)"
                )
            await session.delete(row)
            await session.commit()

        structlog.get_logger().info(
            "runbook_template_discarded",
            tenant_id=str(tenant_id),
            slug=request.slug,
            version=request.version,
        )
        return DiscardTemplateResponse(
            slug=request.slug, version=request.version, status="discarded"
        )

    async def count_in_flight_runs(
        self,
        tenant_id: uuid.UUID,
        slug: str,
        version: int,
    ) -> int:
        """Count ``in_progress`` runs pinned to ``(tenant_id, slug, version)``.

        The same projection :meth:`update_or_fork` reports in
        :attr:`~meho_backplane.runbooks.schemas.ForkInfo.in_flight_run_count`,
        exposed as a standalone read so the console's published-template
        detail view can surface how many runs are still bound to the
        version *before* an admin forks it (forking leaves those runs
        pinned to the source). Tenant-scoped; a cross-tenant / missing
        ``(slug, version)`` simply has zero matching runs and returns ``0``.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            return await _count_in_flight_runs(session, tenant_id, slug, version)

    async def list_templates(
        self,
        tenant_id: uuid.UUID,
        filter_: ListTemplatesFilter,
        limit: int = 100,
    ) -> list[TemplateSummary]:
        """Return the latest version of each slug for *tenant_id*, newest-edited first.

        Each slug shows up once with its latest version *regardless of
        status* (so operators see drafts that have no published version
        yet). The optional ``filter_.status`` / ``filter_.target_kind``
        narrow the set of rows considered before the latest-per-slug
        projection is applied -- a ``status='published'`` filter returns
        the latest *published* version of each slug, skipping slugs whose
        only versions are drafts.

        Ordered by ``edited_at`` descending and capped at *limit* rows.
        """
        if limit < 0:
            raise ValueError(f"limit must be >= 0; got {limit}")
        if limit == 0:
            return []

        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            # Subquery: the max version per slug among the rows that pass
            # the status / target_kind filters. Correlating the outer
            # query against this keeps "latest per slug" portable across
            # SQLite (test) and PostgreSQL (prod) without a window
            # function or DISTINCT ON.
            latest = (
                select(
                    RunbookTemplate.slug.label("slug"),
                    func.max(RunbookTemplate.version).label("version"),
                )
                .where(RunbookTemplate.tenant_id == tenant_id)
                .group_by(RunbookTemplate.slug)
            )
            if filter_.status is not None:
                latest = latest.where(RunbookTemplate.status == filter_.status)
            if filter_.target_kind is not None:
                latest = latest.where(RunbookTemplate.target_kind == filter_.target_kind)
            latest_sub = latest.subquery()

            stmt = (
                select(RunbookTemplate)
                .join(
                    latest_sub,
                    (RunbookTemplate.slug == latest_sub.c.slug)
                    & (RunbookTemplate.version == latest_sub.c.version),
                )
                .where(RunbookTemplate.tenant_id == tenant_id)
                .order_by(RunbookTemplate.edited_at.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()

        return [
            TemplateSummary(
                slug=row.slug,
                version=row.version,
                title=row.title,
                status=_narrow_status(row.status),
                target_kind=row.target_kind,
                edited_at=row.edited_at,
            )
            for row in rows
        ]

    async def show_template(
        self,
        tenant_id: uuid.UUID,
        slug: str,
        version: int | None = None,
    ) -> ShowTemplateResponse:
        """Return the full template body for ``(tenant_id, slug, version)``.

        Resolves the latest version when *version* is ``None``. Returns
        the complete surface including the ordered ``steps``. RBAC is the
        route's job -- the service returns any tenant-scoped row it finds.
        Raises :class:`TemplateNotFoundError` when nothing resolves.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            if version is None:
                resolved = await _resolve_latest_version(session, tenant_id, slug)
                if resolved is None:
                    raise TemplateNotFoundError(f"no template for slug {slug!r} for tenant")
                version = resolved

            row = await self._load_exact(session, tenant_id, slug, version)
            if row is None:
                raise TemplateNotFoundError(f"no template {slug!r} v{version} for tenant")

        return ShowTemplateResponse(
            slug=row.slug,
            version=row.version,
            title=row.title,
            description=row.description,
            target_kind=row.target_kind,
            status=_narrow_status(row.status),
            steps=_steps_from_storage(row.steps),
            created_by=row.created_by,
            created_at=row.created_at,
            edited_by=row.edited_by,
            edited_at=row.edited_at,
        )

    async def _load_draft(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        slug: str,
    ) -> RunbookTemplate | None:
        """Return the single draft row for *slug*, or ``None`` if none exists.

        The versioning invariant (a slug has at most one draft at a time:
        ``create_draft`` refuses a second, ``publish`` consumes the draft)
        means this ``LIMIT 1`` is unambiguous.
        """
        result = await session.execute(
            select(RunbookTemplate)
            .where(
                RunbookTemplate.tenant_id == tenant_id,
                RunbookTemplate.slug == slug,
                RunbookTemplate.status == "draft",
            )
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _load_exact(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        slug: str,
        version: int,
    ) -> RunbookTemplate | None:
        """Return the row for the exact ``(tenant_id, slug, version)`` triple."""
        result = await session.execute(
            select(RunbookTemplate).where(
                RunbookTemplate.tenant_id == tenant_id,
                RunbookTemplate.slug == slug,
                RunbookTemplate.version == version,
            )
        )
        return result.scalar_one_or_none()


async def _resolve_latest_version(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    slug: str,
) -> int | None:
    """Return ``max(version)`` for ``(tenant_id, slug)``, or ``None`` if no rows."""
    latest: int | None = await session.scalar(
        select(func.max(RunbookTemplate.version)).where(
            RunbookTemplate.tenant_id == tenant_id,
            RunbookTemplate.slug == slug,
        )
    )
    return latest


async def _count_in_flight_runs(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    slug: str,
    version: int,
) -> int:
    """Count ``in_progress`` runs pinned to ``(tenant_id, slug, version)``.

    The only read of ``runbook_runs`` from the template-side service.
    G12.3's run service is the durable owner of the table; this read
    feeds the senior's fork decision (how many runs are still bound to
    the version being forked).
    """
    count = await session.scalar(
        select(func.count())
        .select_from(RunbookRun)
        .where(
            RunbookRun.tenant_id == tenant_id,
            RunbookRun.template_slug == slug,
            RunbookRun.template_version == version,
            RunbookRun.state == _IN_PROGRESS_STATE,
        )
    )
    return count or 0


def _steps_to_storage(steps: list[Step]) -> list[dict[str, object]]:
    """Serialise validated Pydantic steps to the ``steps`` JSONB column shape."""
    return [step.model_dump(mode="json") for step in steps]


def _steps_from_storage(steps: list[dict[str, object]]) -> list[Step]:
    """Re-validate stored step dicts back into the discriminated-union models.

    The round-trip through :class:`RunbookTemplateBody` reuses the
    template-level validators (step-id uniqueness, substitution
    allowlist) so a row that somehow reached storage malformed surfaces
    at read time rather than leaking an unvalidated shape to the caller.
    """
    return RunbookTemplateBody(
        title="",
        description="",
        target_kind=None,
        steps=steps,  # type: ignore[arg-type]
    ).steps


def _narrow_status(status: str) -> _TemplateStatus:
    """Narrow the ``str`` status column to the ``Literal`` the responses expect.

    The DB ``CheckConstraint`` on ``runbook_templates.status`` guarantees
    one of ``draft`` / ``published`` / ``deprecated`` at write time. This
    helper re-asserts that closed set at read time so an out-of-vocabulary
    value (a hand-edited row, a future migration gap) surfaces as a clean
    ``ValueError`` here rather than as a Pydantic validation error one
    layer up -- and keeps the ``str -> Literal`` narrowing localised for
    the type checker.
    """
    if status not in _TEMPLATE_STATUSES:
        raise ValueError(f"unexpected template status: {status!r}")
    return status  # type: ignore[return-value]
