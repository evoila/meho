# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Resolver for the universal ``target`` → ``Target`` ORM row lookup.

:func:`resolve_target` is called by every route and CLI verb that accepts
a ``--target`` / ``target`` parameter. The algorithm (from consumer-needs.md
§G3 and the consumer's #110 fix) is:

1. Exact name match for the tenant — ``WHERE tenant_id = ? AND name = ?
   AND deleted_at IS NULL``. If unique, return immediately.
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

Every clause filters ``deleted_at IS NULL`` so soft-deleted targets
(G0.14-T4 #1145) are invisible to the resolver. The DELETE handler
stamps ``deleted_at`` and lets the row stay queryable from the
:attr:`AuditLog.target_id` soft-FK, but every dispatch / probe /
list / CLI verb that goes through the resolver sees the row as
"not found" with the live near-misses surfaced exactly as before.

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
                "matches": [m.model_dump(mode="json") for m in matches],
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
                "matches": [m.model_dump(mode="json") for m in matches],
            },
        )


# code-quality-allow: function-size — linear three-phase resolver
# (exact → alias → near-miss) where each phase consumes the prior's None
# result and the trailing bind requires the resolved row; decomposition
# forces sentinel returns or raise-mid-helper.
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

    On success the resolved target's ``id`` is bound into structlog's
    contextvars under the key ``target_id`` (G0.3-T4). The
    :class:`~meho_backplane.audit.AuditMiddleware` reads this value at
    audit-write time and writes it to ``audit_log.target_id``. Binding
    happens here — the single canonical exit point — rather than in each
    caller, so no route can accidentally skip the binding.

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
    target: TargetORM | None = None

    # Step 1: exact name match.
    # Use limit(2) instead of scalar_one_or_none() so data-drift duplicates
    # (unique index violation repaired mid-flight, or a restored backup with
    # a relaxed constraint) raise AmbiguousTargetError (409) rather than
    # leaking MultipleResultsFound as an unhandled 500.
    # ``deleted_at IS NULL`` filter (G0.14-T4 #1145) excludes soft-deleted
    # rows so a re-creation under the same name does not collide with a
    # tombstone, and a stale CLI cache referencing a deleted name resolves
    # to the standard 404 (rather than returning the deleted row).
    stmt = select(TargetORM).where(
        TargetORM.tenant_id == tenant_id,
        TargetORM.name == query,
        TargetORM.deleted_at.is_(None),
    )
    result = await session.execute(stmt.limit(2))
    exact_hits = list(result.scalars().all())
    if len(exact_hits) == 1:
        target = exact_hits[0]
    elif len(exact_hits) > 1:
        summaries = [_to_summary(t) for t in exact_hits]
        _log.warning(
            "ambiguous_exact_name",
            tenant_id=str(tenant_id),
            query=query,
            matches=[s.name for s in summaries],
        )
        raise AmbiguousTargetError(query, summaries)
    else:
        target = None

    if target is None:
        # Step 2: alias match — dialect-aware.
        alias_hits = await _alias_match(session, tenant_id, query)
        if len(alias_hits) == 1:
            target = alias_hits[0]
        elif len(alias_hits) > 1:
            summaries = [_to_summary(t) for t in alias_hits]
            _log.warning(
                "ambiguous_target",
                tenant_id=str(tenant_id),
                query=query,
                matches=[s.name for s in summaries],
            )
            raise AmbiguousTargetError(query, summaries)

    if target is None:
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

    # Single exit point — bind target_id for AuditMiddleware (G0.3-T4) and
    # target_name for the MCP outer-wrapper row's payload (G0.15-T3 #1212).
    # The HTTP audit middleware writes only the typed target_id column; the
    # MCP path additionally drops the canonical name into payload so
    # ``query_audit target=<name>`` matches the MCP envelope row as well as
    # the inner DISPATCH row. Binding here — the single canonical exit
    # point of name-or-alias resolution — keeps the value consistent with
    # the resolved row's identity (aliases collapse to the canonical name).
    structlog.contextvars.bind_contextvars(
        target_id=str(target.id),
        target_name=target.name,
    )
    _log.info("target_resolved", target_id=str(target.id), name=target.name)
    return target


async def _alias_match(
    session: AsyncSession,
    tenant_id: UUID,
    query: str,
) -> list[TargetORM]:
    """Return live targets whose ``aliases`` contain *query* as an exact element.

    ``deleted_at IS NULL`` filter (G0.14-T4 #1145) excludes soft-deleted
    rows so an alias on a retired target does not shadow a live re-creation
    holding the same alias.
    """
    conn = await session.connection()
    if conn.dialect.name == "postgresql":
        stmt = select(TargetORM).where(
            TargetORM.tenant_id == tenant_id,
            TargetORM.deleted_at.is_(None),
            text(":q = ANY(aliases)").bindparams(q=query),
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())
    # Non-PG (SQLite dev/test): load all live tenant targets, filter in Python.
    # Aliases is a list[str] column; element-equality is a Python `in` check.
    stmt = select(TargetORM).where(
        TargetORM.tenant_id == tenant_id,
        TargetORM.deleted_at.is_(None),
    )
    result = await session.execute(stmt)
    return [t for t in result.scalars().all() if query in t.aliases]


async def _near_misses(
    session: AsyncSession,
    tenant_id: UUID,
    query: str,
) -> list[TargetORM]:
    """Return up to 5 live near-miss targets for the 404 ``matches`` field.

    ``deleted_at IS NULL`` filter (G0.14-T4 #1145) keeps near-miss
    suggestions to live targets only so an operator typo-correcting
    against a recent deletion is not pointed at the tombstone.
    """
    conn = await session.connection()
    prefix = f"{query}%"
    if conn.dialect.name == "postgresql":
        stmt = (
            select(TargetORM)
            .where(
                TargetORM.tenant_id == tenant_id,
                TargetORM.deleted_at.is_(None),
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
                TargetORM.deleted_at.is_(None),
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
        aliases=tuple(t.aliases),
        product=t.product,
        host=t.host,
    )
