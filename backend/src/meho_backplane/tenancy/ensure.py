# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Just-in-time tenant seeding from the verified ``tenant_id`` claim.

A fresh v0.2 deploy ships an empty ``tenant`` table (migration
``0002`` creates the table as the FK keystone but defers seeding to a
future release). Every tenant-scoped write — ``documents``,
``graph_node``, ``graph_edge``, ``broadcast_override`` — carries a
``tenant_id`` lifted from the operator's JWT and fails
``documents_tenant_id_fkey`` (and its siblings) until a matching
``tenant`` row exists. Nothing in the request path or the published
runbooks seeded it, so the first *real* (non-dry-run) write on a
runbook-following deploy hit an unhandled asyncpg
``ForeignKeyViolationError``.

:func:`ensure_tenant` is the minimal fix: an idempotent get-or-create
issued once per authenticated-request operator context, keyed to the
verified ``tenant_id`` claim. ``slug`` and ``name`` derive
deterministically from the UUID; a future v0.3 tenant-provisioning API
can rename them out of band (``slug`` carries no FK and the
get-or-create never overwrites an existing row).

The statement is a single dialect-native
``INSERT ... ON CONFLICT (id) DO NOTHING``. ``ON CONFLICT DO NOTHING``
makes concurrent first-writes safe: N requests racing on the same
fresh ``tenant_id`` all attempt the insert, exactly one row lands, the
losers no-op without raising. No ``SELECT``-then-``INSERT`` race
window, no advisory lock, no application-level dedupe.

JIT provisioning from a verified IdP claim is the standard
multi-tenant pattern — an Auth0/Keycloak-fronted backend provisions
the tenant row on first authenticated request rather than requiring a
separate out-of-band seed step.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.sql.dml import Insert

from meho_backplane.db.models import Tenant

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["ensure_tenant"]

_log = structlog.get_logger(__name__)


def _derive_slug(tenant_id: UUID) -> str:
    """Deterministic placeholder slug for a JIT-seeded tenant.

    ``tenant-<first-8-hex>`` — short enough to be operator-legible in
    logs and admin queries, deterministic so re-deriving it for the
    same ``tenant_id`` is stable, and namespaced under ``tenant-`` so
    a future v0.3 provisioning API can tell auto-seeded rows from
    operator-named ones at a glance. The slug carries no FK, so a
    later rename is a single ``UPDATE`` with no cascade.
    """
    return f"tenant-{tenant_id.hex[:8]}"


async def ensure_tenant(tenant_id: UUID, session: AsyncSession) -> None:
    """Idempotently ensure a ``tenant`` row exists for *tenant_id*.

    Issues a single dialect-native
    ``INSERT INTO tenant (id, slug, name) VALUES (...)
    ON CONFLICT (id) DO NOTHING``. Calling it N times for the same
    *tenant_id* — including concurrently — leaves exactly one row and
    never overwrites an existing row's ``slug`` / ``name`` (a v0.3
    provisioning API may rename them; this path must not clobber that).

    The dialect is resolved from the session's bound connection, the
    same idiom the targets resolver uses
    (``conn.dialect.name``). Both the PostgreSQL and SQLite dialects
    expose ``on_conflict_do_nothing()``; called with no arguments it
    targets the primary key (``tenant.id``), which is exactly the
    idempotency key here. The generic Core ``insert()`` has no
    ``on_conflict`` clause, so the dialect-specific constructor is
    required on each path — there is no portable single-statement
    form.

    Args:
        tenant_id: The verified tenant UUID from the operator's JWT
            claim. Caller is responsible for having validated the
            claim (``verify_jwt`` rejects a missing/malformed claim
            at 401 before this is reached).
        session: An open async session. The caller owns the
            transaction boundary — this helper neither commits nor
            rolls back, matching the surrounding request-scoped
            session lifecycle.
    """
    slug = _derive_slug(tenant_id)
    values = {"id": tenant_id, "slug": slug, "name": slug}

    conn = await session.connection()
    # The PG and SQLite dialect ``Insert`` types are distinct concrete
    # classes; annotate against their shared ``sqlalchemy.sql.dml.Insert``
    # base so both branches assign to the same statically-typed name.
    stmt: Insert
    if conn.dialect.name == "postgresql":
        stmt = (
            pg_insert(Tenant)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=["id"],
            )
        )
    else:
        # SQLite (dev/test via aiosqlite) — the sqlite dialect's
        # on_conflict_do_nothing has no `constraint` kwarg but
        # accepts `index_elements`; targeting `id` matches the PG
        # path's conflict target exactly.
        stmt = (
            sqlite_insert(Tenant)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=["id"],
            )
        )

    result = await session.execute(stmt)
    # SQLAlchemy 2.x types DML execute() as ``Result[Any]`` whose
    # ``rowcount`` is only typed on the concrete ``CursorResult``
    # subclass; the DML path returns a cursor-shaped result on every
    # supported driver (asyncpg + aiosqlite) so the bare access is
    # correct at runtime — same idiom :mod:`meho_backplane.kb.service`
    # uses. ``rowcount`` is 1 on the seeding insert, 0 when the row
    # already existed (conflict → no-op).
    rowcount: int = result.rowcount  # type: ignore[attr-defined]
    if rowcount:
        # Logged at info on the seed so an operator watching a fresh
        # deploy sees the first-write provisioning happen; the no-op
        # case is silent (every subsequent authenticated request would
        # otherwise spam it).
        _log.info("tenant_seeded", tenant_id=str(tenant_id), slug=slug)
