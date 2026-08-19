# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Resolvers for the ``event_source`` registry (#2880).

Two lookups, both filtering ``deleted_at IS NULL`` so a soft-deleted
source is invisible:

* :func:`resolve_event_source` -- the **admin** lookup, tenant-scoped by
  ``slug``. Used by the REST / CLI / UI CRUD handlers. A slug that does
  not exist *or* belongs to another tenant raises the identical
  :class:`EventSourceNotFoundError` (404 ``no_event_source``): the two are
  conflated so the admin surface reveals no cross-tenant existence oracle
  (the :mod:`meho_backplane.auth.runner_guard` discipline). Unlike
  :func:`~meho_backplane.targets.resolver.resolve_target` it surfaces **no**
  near-miss ``matches`` list -- a slug is an opaque routing token, not a
  human handle worth suggesting corrections for, and suggestions would
  themselves be an oracle.

* :func:`resolve_event_source_by_slug` -- the **ingest** primitive
  (consumed by #2881), *not* tenant-scoped: a JWT-less webhook carries no
  tenant, so slug is resolved globally against the unique
  ``event_source_slug_idx``. Returns the live row (of any status) or
  ``None``; the ingest endpoint reads ``status`` / ``auth_strategy`` /
  ``secret_ref`` from it and MUST map a ``None`` result to the *same*
  uniform response it returns for an authentication failure, so a missing
  slug is indistinguishable from a bad signature to the sender. Returning
  a bare ``None`` (no exception, no near-miss detail) keeps that discipline
  at the primitive level.
"""

from __future__ import annotations

from uuid import UUID

import structlog
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import EventSource as EventSourceORM

__all__ = [
    "EventSourceNotFoundError",
    "resolve_event_source",
    "resolve_event_source_by_slug",
]

_log = structlog.get_logger(__name__)


class EventSourceNotFoundError(HTTPException):
    """Raised when no live source matches the slug within the tenant.

    The detail carries the queried slug but **no** ``matches`` list: the
    admin surface must not distinguish "no such slug" from "slug owned by
    another tenant" or leak near-miss suggestions (existence-oracle
    discipline). A cross-tenant slug therefore returns exactly this 404.
    """

    def __init__(self, slug: str) -> None:
        super().__init__(
            status_code=404,
            detail={"error": "no_event_source", "slug": slug},
        )


async def resolve_event_source(
    session: AsyncSession,
    tenant_id: UUID,
    slug: str,
) -> EventSourceORM:
    """Resolve a live event source by slug, tenant-scoped.

    Returns the :class:`~meho_backplane.db.models.EventSource` ORM row for
    ``(tenant_id, slug)`` where ``deleted_at IS NULL``. Raises
    :class:`EventSourceNotFoundError` (uniform 404) when the slug does not
    resolve in this tenant -- whether it is truly absent or belongs to
    another tenant.
    """
    stmt = select(EventSourceORM).where(
        EventSourceORM.tenant_id == tenant_id,
        EventSourceORM.slug == slug,
        EventSourceORM.deleted_at.is_(None),
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        # Log the miss server-side with the distinguishing reason so an
        # operator can debug, while the client sees only the uniform 404.
        _log.info("event_source_not_found", tenant_id=str(tenant_id), slug=slug)
        raise EventSourceNotFoundError(slug)
    return row


async def resolve_event_source_by_slug(
    session: AsyncSession,
    slug: str,
) -> EventSourceORM | None:
    """Resolve a live event source by slug globally (ingest primitive).

    Not tenant-scoped: the caller (the #2881 ingest endpoint) has no
    tenant context, so slug is matched globally against the unique
    ``event_source_slug_idx``. Returns the live row (any ``status``) or
    ``None`` -- never an exception, never near-miss detail. The caller is
    responsible for mapping ``None`` to the same uniform response it uses
    for an authentication failure so the lookup reveals no existence
    oracle.
    """
    stmt = select(EventSourceORM).where(
        EventSourceORM.slug == slug,
        EventSourceORM.deleted_at.is_(None),
    )
    return (await session.execute(stmt)).scalar_one_or_none()
