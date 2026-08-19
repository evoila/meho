# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""REST admin surface for the ``event_source`` registry (#2880).

Tenant-scoped CRUD over ``/api/v1/event-sources`` -- the human/agent
front the CLI (``meho event-source ...``) and the UI both dispatch
through. Reads require ``operator``; writes require ``tenant_admin``
(mirrors :mod:`meho_backplane.api.v1.targets`). ``tenant_id`` is always
taken from the JWT; the body can never set it, and a source is addressed
by its URL-safe ``slug`` (tenant-scoped -- a cross-tenant slug returns the
same uniform 404 as a missing one, so the surface leaks no existence
oracle).

Secret custody is the one behaviour beyond a plain registry (#2880
acceptance criterion 2): a create/update carrying a ``secret`` writes it
to Vault at a derived per-tenant ``secret_ref`` and persists only the
path. The value is a :class:`~pydantic.SecretStr` end to end and never
enters the row, a log line, or the audit trail. A Vault write failure
fails the request closed (502) so a source is never registered pointing
at a secret that was not stored.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.db.engine import get_session
from meho_backplane.db.models import EventSource as EventSourceORM
from meho_backplane.event_source.resolver import resolve_event_source
from meho_backplane.event_source.schemas import (
    EventSource,
    EventSourceCreate,
    EventSourceListResponse,
    EventSourceStatus,
    EventSourceUpdate,
    project_event_source_to_summary,
)
from meho_backplane.event_source.secrets import (
    event_source_secret_ref,
    store_event_source_secret,
)

__all__ = ["router"]

_log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/event-sources", tags=["event-sources"])

#: Module-level Depends closures -- required to satisfy ruff B008 (mutable
#: calls in default argument positions). Mirrors :mod:`meho_backplane.api.v1.targets`.
_require_operator = Depends(require_role(TenantRole.OPERATOR))
_require_admin = Depends(require_role(TenantRole.TENANT_ADMIN))

#: List page bounds (mirrors the targets list contract).
_DEFAULT_LIMIT = 100
_MAX_LIMIT = 500


def _to_full(row: EventSourceORM) -> EventSource:
    """Project an ``EventSource`` ORM row to the full read shape."""
    return EventSource.model_validate(row, from_attributes=True)


async def _store_secret_or_502(operator: Operator, secret_ref: str, secret: SecretStr) -> None:
    """Write *secret* to Vault at *secret_ref*, failing the request closed.

    Any Vault-side failure raises 502 ``secret_store_failed`` and leaves
    the transaction to roll back, so the source is never persisted (create)
    or the rotation is never acknowledged (update) while the secret sits
    unstored. The secret value never reaches the response or the log: the
    sink names only the path/field, and :class:`~pydantic.SecretStr` /
    ``SecretMaterial`` redact the value in any traceback frame.
    """
    try:
        await store_event_source_secret(operator, secret_ref, secret)
    except Exception as exc:  # any Vault failure must fail the write closed
        # Log a structured line, not the full traceback: the frame locals
        # include the ``secret`` parameter, and even its redacted
        # ``SecretStr('**********')`` rendering would trip the audit
        # secret-leak sweep on the literal ``secret =`` substring. The
        # error class + secret_ref is the operationally useful signal.
        _log.error(
            "event_source_secret_store_failed",
            secret_ref=secret_ref,
            error_class=type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "secret_store_failed",
                "message": (
                    "failed to store the event source secret in Vault; "
                    "the registry write was rolled back"
                ),
            },
        ) from exc


@router.get("", response_model=EventSourceListResponse)
async def list_event_sources(
    operator: Operator = _require_operator,
    session: AsyncSession = Depends(get_session),
    status_filter: EventSourceStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    cursor: str | None = Query(default=None),
) -> EventSourceListResponse:
    """List the tenant's live event sources, keyset-paginated by ``name``.

    Optional ``?status=active|paused`` filter. ``?cursor=<name>`` continues
    after a previous page's ``next_cursor``. Soft-deleted sources are
    excluded.
    """
    stmt = select(EventSourceORM).where(
        EventSourceORM.tenant_id == operator.tenant_id,
        EventSourceORM.deleted_at.is_(None),
    )
    if status_filter is not None:
        stmt = stmt.where(EventSourceORM.status == status_filter.value)
    if cursor is not None:
        stmt = stmt.where(EventSourceORM.name > cursor)
    stmt = stmt.order_by(EventSourceORM.name).limit(limit + 1)
    rows = list((await session.execute(stmt)).scalars().all())
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = page[-1].name if has_more else None
    return EventSourceListResponse(
        items=[project_event_source_to_summary(r) for r in page],
        next_cursor=next_cursor,
    )


@router.get("/{slug}", response_model=EventSource)
async def describe_event_source(
    slug: str,
    operator: Operator = _require_operator,
    session: AsyncSession = Depends(get_session),
) -> EventSource:
    """Describe one event source by slug. 404 (uniform) when it does not
    resolve in the tenant."""
    row = await resolve_event_source(session, operator.tenant_id, slug)
    return _to_full(row)


@router.post("", response_model=EventSource, status_code=201)
async def create_event_source(
    body: EventSourceCreate,
    operator: Operator = _require_admin,
    session: AsyncSession = Depends(get_session),
) -> EventSource:
    """Register a new event source in the requesting tenant.

    Returns 409 when the ``name`` (per tenant) or ``slug`` (global) is
    already in use by a live source. When ``secret`` is supplied it is
    written to Vault at the derived per-tenant ``secret_ref`` after the
    row flushes (so a duplicate name/slug 409s before any Vault write) and
    before the transaction commits (so a Vault failure rolls the row back).
    Omitting ``secret`` registers the source with no credential
    (``secret_ref`` NULL); the ingest path (#2881) fails closed until a
    later PATCH sets one.
    """
    now = datetime.now(UTC)
    secret_ref = (
        event_source_secret_ref(operator.tenant_id, body.slug) if body.secret is not None else None
    )
    row = EventSourceORM(
        id=uuid.uuid4(),
        tenant_id=operator.tenant_id,
        name=body.name,
        slug=body.slug,
        kind=body.kind.value,
        auth_strategy=body.auth_strategy.value,
        secret_ref=secret_ref,
        status=body.status.value,
        extras=body.extras,
        created_by_sub=operator.sub,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"event source name {body.name!r} or slug {body.slug!r} is already in use",
        ) from exc
    if body.secret is not None:
        assert secret_ref is not None  # derived in lockstep with body.secret
        await _store_secret_or_502(operator, secret_ref, body.secret)
    _log.info(
        "event_source_created",
        event_source_id=str(row.id),
        slug=row.slug,
        kind=row.kind,
        tenant_id=str(operator.tenant_id),
        has_secret=body.secret is not None,
    )
    return _to_full(row)


@router.patch("/{slug}", response_model=EventSource)
async def update_event_source(
    slug: str,
    body: EventSourceUpdate,
    operator: Operator = _require_admin,
    session: AsyncSession = Depends(get_session),
) -> EventSource:
    """Partially update an event source.

    Only fields present in the body are changed; ``name`` and ``slug`` are
    immutable (re-create to change them, since ``slug`` is a live routing
    key). A PATCH to ``status='paused'`` takes effect on the ingest path's
    next lookup -- nothing caches the row. Sending ``secret`` rotates the
    Vault secret at the source's existing ``secret_ref`` (a new KV-v2
    version at the same derived path, so no downtime); it is also how an
    operator sets the first secret on a source created without one.
    """
    row = await resolve_event_source(session, operator.tenant_id, slug)
    updates = body.model_dump(exclude_unset=True, exclude={"secret"})
    for key, value in updates.items():
        # StrEnum values persist as their plain string (Text column).
        setattr(row, key, value.value if hasattr(value, "value") else value)
    if body.secret is not None:
        # slug is immutable, so the derived ref equals any existing one;
        # this also (re)homes secret_ref when the source had none.
        secret_ref = event_source_secret_ref(operator.tenant_id, row.slug)
        await _store_secret_or_502(operator, secret_ref, body.secret)
        row.secret_ref = secret_ref
    row.updated_at = datetime.now(UTC)
    _log.info(
        "event_source_updated",
        event_source_id=str(row.id),
        slug=row.slug,
        tenant_id=str(operator.tenant_id),
        fields=list(updates.keys()),
        secret_rotated=body.secret is not None,
    )
    return _to_full(row)


@router.delete("/{slug}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_event_source(
    slug: str,
    operator: Operator = _require_admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Soft-delete an event source by slug.

    Stamps ``deleted_at`` so the row stays for audit history while every
    read path (and the ingest resolver) filters it out; the partial unique
    indexes free the ``name`` and ``slug`` for re-use immediately. The
    Vault secret is intentionally left in place (RDC owns the Vault
    lifecycle; a re-created source with the same slug derives the same ref
    and overwrites it on its next secret write). A second DELETE collapses
    to the uniform 404 -- the row no longer resolves.
    """
    row = await resolve_event_source(session, operator.tenant_id, slug)
    row.deleted_at = datetime.now(UTC)
    await session.flush()
    _log.info(
        "event_source_deleted",
        event_source_id=str(row.id),
        slug=row.slug,
        tenant_id=str(operator.tenant_id),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
