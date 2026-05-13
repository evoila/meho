# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Resolver for the universal ``target`` → ``Target`` ORM row lookup.

:func:`resolve_target` is called by every route and CLI verb that accepts
a ``--target`` / ``target`` parameter. The algorithm (from consumer-needs.md
§G3 and the consumer's #110 fix) is:

1. Exact name match for the tenant — ``WHERE tenant_id = ? AND name = ?``.
   If unique, return immediately.
2. Element-equality alias match — ``query = ANY(aliases)`` on PostgreSQL,
   Python-side set-membership on other dialects (SQLite dev/test path).
   The consumer's #110 incident showed that substring matching caused
   ``--target k8s`` to resolve to either ``rke2-meho`` or ``rke2-infra``
   ambiguously; element-equality is the correct semantic.
3. If steps 1 and 2 both return nothing, run a prefix-ILIKE near-miss
   query (up to 5 candidates) and raise :exc:`TargetNotFoundError` with
   the candidates in the ``matches`` field.
4. If step 1 or 2 returns more than one row (defensive — the unique index
   prevents it under normal conditions), raise :exc:`AmbiguousTargetError`.

Dialect portability
-------------------

``= ANY(aliases)`` and ``unnest(aliases)`` are PostgreSQL-specific.
On non-PG dialects (SQLite for unit tests) the alias step loads all
tenant targets and filters in Python; the near-miss step falls back to
name-only ILIKE (no ``unnest``). Both paths return identical results;
only the I/O efficiency differs.

Error shape
-----------

Both error classes extend :class:`fastapi.HTTPException` so they
propagate cleanly through FastAPI route handlers. CLI verbs catch them
and render human-readable output.
"""

from __future__ import annotations

from uuid import UUID

import structlog
from fastapi import HTTPException
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import Target as TargetORM
from meho_backplane.targets.schemas import TargetSummary

__all__ = ["AmbiguousTargetError", "TargetNotFoundError", "resolve_target"]

_log = structlog.get_logger(__name__)


class TargetNotFoundError(HTTPException):
    """Raised when no target matches the query within the tenant.

    ``matches`` is a list of :class:`TargetSummary` instances that are
    near-misses (prefix-ILIKE on name; on PG also on aliases). CLI
    verbs render these as suggestions; API routes serialise them via
    ``detail["matches"]``.
    """

    def __init__(self, query: str, matches: list[TargetSummary]) -> None:
        super().__init__(
            status_code=404,
            detail={
                "error": "no_target",
                "query": query,
                "matches": [m.model_dump() for m in matches],
            },
        )


class AmbiguousTargetError(HTTPException):
    """Raised when multiple targets match the query (defensive).

    Under normal operation the ``(tenant_id, name)`` unique index
    prevents exact-name duplicates. This error surfaces if the alias
    step returns more than one row, which the unique index does not
    prevent (two targets can have the same alias). The ``matches``
    field lists all hits so the caller can surface them to the user.
    """

    def __init__(self, query: str, matches: list[TargetSummary]) -> None:
        super().__init__(
            status_code=409,
            detail={
                "error": "ambiguous_target",
                "query": query,
                "matches": [m.model_dump() for m in matches],
            },
        )


async def resolve_target(
    session: AsyncSession,
    tenant_id: UUID,
    query: str,
) -> TargetORM:
    """Resolve a name-or-alias query to a :class:`TargetORM` row, tenant-scoped.

    See module docstring for the full algorithm. Returns the matching
    :class:`~meho_backplane.db.models.Target` ORM instance; callers that
    need the Pydantic read shape convert with
    ``Target.model_validate(row, from_attributes=True)``.

    Args:
        session: Active async DB session (must be inside an open transaction).
        tenant_id: The tenant scope — only targets belonging to this tenant
            are considered.
        query: The name or alias to resolve.

    Returns:
        The matching :class:`~meho_backplane.db.models.Target` ORM row.

    Raises:
        TargetNotFoundError: No target matches *query* in *tenant_id*.
        AmbiguousTargetError: Multiple targets match *query* (alias collision).
    """
    # Step 1: exact name match.
    stmt = select(TargetORM).where(
        TargetORM.tenant_id == tenant_id,
        TargetORM.name == query,
    )
    result = await session.execute(stmt)
    exact = result.scalar_one_or_none()
    if exact is not None:
        return exact

    # Step 2: alias match — dialect-aware.
    alias_hits = await _alias_match(session, tenant_id, query)
    if len(alias_hits) == 1:
        return alias_hits[0]
    if len(alias_hits) > 1:
        summaries = [_to_summary(t) for t in alias_hits]
        _log.warning(
            "ambiguous_target",
            tenant_id=str(tenant_id),
            query=query,
            matches=[s.name for s in summaries],
        )
        raise AmbiguousTargetError(query, summaries)

    # Step 3: near-miss for 404 detail.
    near = await _near_misses(session, tenant_id, query)
    summaries = [_to_summary(t) for t in near]
    _log.info(
        "target_not_found",
        tenant_id=str(tenant_id),
        query=query,
        near_misses=[s.name for s in summaries],
    )
    raise TargetNotFoundError(query, summaries)


async def _alias_match(
    session: AsyncSession,
    tenant_id: UUID,
    query: str,
) -> list[TargetORM]:
    """Return targets whose ``aliases`` contain *query* as an exact element."""
    conn = await session.connection()
    if conn.dialect.name == "postgresql":
        stmt = select(TargetORM).where(
            TargetORM.tenant_id == tenant_id,
            text(":q = ANY(aliases)").bindparams(q=query),
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())
    # Non-PG (SQLite dev/test): load all tenant targets, filter in Python.
    # Aliases is a list[str] column; element-equality is a Python `in` check.
    stmt = select(TargetORM).where(TargetORM.tenant_id == tenant_id)
    result = await session.execute(stmt)
    return [t for t in result.scalars().all() if query in t.aliases]


async def _near_misses(
    session: AsyncSession,
    tenant_id: UUID,
    query: str,
) -> list[TargetORM]:
    """Return up to 5 near-miss targets for the 404 ``matches`` field."""
    conn = await session.connection()
    prefix = f"{query}%"
    if conn.dialect.name == "postgresql":
        stmt = (
            select(TargetORM)
            .where(
                TargetORM.tenant_id == tenant_id,
                or_(
                    TargetORM.name.ilike(prefix),
                    text("EXISTS (SELECT 1 FROM unnest(aliases) AS a WHERE a ILIKE :p)").bindparams(
                        p=prefix
                    ),
                ),
            )
            .limit(5)
        )
    else:
        # SQLite: name ILIKE only (no unnest).
        stmt = (
            select(TargetORM)
            .where(
                TargetORM.tenant_id == tenant_id,
                TargetORM.name.ilike(prefix),
            )
            .limit(5)
        )
    result = await session.execute(stmt)
    return list(result.scalars().all())


def _to_summary(t: TargetORM) -> TargetSummary:
    return TargetSummary(
        id=t.id,
        name=t.name,
        aliases=t.aliases,
        product=t.product,
        host=t.host,
    )
