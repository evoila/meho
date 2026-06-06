# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Resolver for the ``collection_key`` → :class:`DocCollection` ORM lookup.

:func:`resolve_doc_collection` is the single entry point the
collection-scoped ``search_docs`` / ``ask_docs`` path (T3 #1552) and
the catalogue tool (T4 #1553) use to turn an operator-supplied
``collection`` key into the registry row that binds it to a backend.

Tenant-then-global fallback
---------------------------

A ``collection_key`` may exist as a global / shared row
(``tenant_id IS NULL``, available to every tenant) and as a
tenant-curated row (``tenant_id = <tenant>``). The dual partial unique
indexes (migration 0037) let both coexist; the resolver **prefers the
tenant row** so a tenant can override a shared collection's backend
binding or metadata without renaming it. This mirrors the
global-vs-tenant visibility the operation-registry lookups enforce
(``(tenant_id IS NULL) OR (tenant_id = :tenant)`` in
:mod:`meho_backplane.operations._lookup`).

A caller that needs only the global row (ignoring any tenant override)
is out of scope for T1; the agent-facing path is always tenant-scoped.

Error shape
-----------

An unknown key raises :exc:`DocCollectionNotFoundError`, which extends
:class:`fastapi.HTTPException` (status 404) so it propagates cleanly
through FastAPI route handlers and is catchable by CLI verbs that
render human-readable output. The error detail carries the catalogue
of keys visible to the tenant (global + tenant-curated) so the caller
can surface "did you mean…?" without a second query — this is the
typed not-found the acceptance criteria require.
"""

from __future__ import annotations

from uuid import UUID

import structlog
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import DocCollection as DocCollectionORM

__all__ = [
    "DocCollectionNotFoundError",
    "resolve_doc_collection",
]

_log = structlog.get_logger(__name__)


class DocCollectionNotFoundError(HTTPException):
    """Raised when no doc collection matches *collection_key* for the tenant.

    ``known_keys`` lists the collection keys visible to the tenant
    (global rows + this tenant's own rows) so the caller can render
    suggestions. API routes serialise it via ``detail["known_keys"]``;
    CLI verbs render it as a "available collections" hint.
    """

    def __init__(self, collection_key: str, known_keys: list[str]) -> None:
        super().__init__(
            status_code=404,
            detail={
                "error": "no_doc_collection",
                "collection_key": collection_key,
                "known_keys": known_keys,
            },
        )


async def resolve_doc_collection(
    session: AsyncSession,
    collection_key: str,
    tenant_id: UUID,
) -> DocCollectionORM:
    """Resolve *collection_key* to a :class:`DocCollectionORM` row, tenant-first.

    Prefers the tenant-curated row (``tenant_id = tenant_id``) when one
    exists, falling back to the global / shared row
    (``tenant_id IS NULL``). See the module docstring for the rationale.

    Args:
        session: Active async DB session (inside an open transaction).
        collection_key: The stable collection id, e.g. ``"vmware"``.
        tenant_id: The tenant scope. The tenant's own row wins over the
            global row with the same key.

    Returns:
        The matching :class:`~meho_backplane.db.models.DocCollection`
        ORM row. Callers needing the Pydantic read shape convert with
        ``DocCollection.model_validate(row, from_attributes=True)``.

    Raises:
        DocCollectionNotFoundError: No collection with *collection_key*
            is visible to *tenant_id* (neither tenant-curated nor
            global).
    """
    # Pull both the tenant-curated and the global row for the key in one
    # query, then prefer the tenant row in Python. The dual partial
    # unique indexes guarantee at most one row per (scope, key), so this
    # returns at most two rows.
    stmt = select(DocCollectionORM).where(
        DocCollectionORM.collection_key == collection_key,
        (DocCollectionORM.tenant_id == tenant_id) | (DocCollectionORM.tenant_id.is_(None)),
    )
    result = await session.execute(stmt)
    rows = list(result.scalars().all())

    tenant_row = next((r for r in rows if r.tenant_id == tenant_id), None)
    global_row = next((r for r in rows if r.tenant_id is None), None)
    collection = tenant_row or global_row

    if collection is None:
        known_keys = await _known_keys(session, tenant_id)
        _log.info(
            "doc_collection_not_found",
            tenant_id=str(tenant_id),
            collection_key=collection_key,
            known_keys=known_keys,
        )
        raise DocCollectionNotFoundError(collection_key, known_keys)

    _log.info(
        "doc_collection_resolved",
        tenant_id=str(tenant_id),
        collection_key=collection.collection_key,
        scope="tenant" if collection.tenant_id is not None else "global",
    )
    return collection


async def _known_keys(session: AsyncSession, tenant_id: UUID) -> list[str]:
    """Return the sorted collection keys visible to *tenant_id*.

    Global rows (``tenant_id IS NULL``) plus this tenant's own rows.
    A tenant-curated key that shadows a global key appears once
    (deduplicated) — the catalogue answer is "this key exists", not
    "in how many scopes".
    """
    stmt = select(DocCollectionORM.collection_key).where(
        (DocCollectionORM.tenant_id == tenant_id) | (DocCollectionORM.tenant_id.is_(None))
    )
    result = await session.execute(stmt)
    return sorted({str(key) for key in result.scalars().all()})
