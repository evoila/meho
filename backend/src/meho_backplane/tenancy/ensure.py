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
runbooks seeded it, so the first real write on a runbook-following
deploy hit an unhandled asyncpg ``ForeignKeyViolationError``.

:func:`ensure_tenant` is the minimal fix: an idempotent get-or-create
issued once per authenticated request from the
``verify_jwt_and_bind`` middleware every authenticated route flows
through — so the *first authenticated request* of any kind (reads and
``dry_run`` included), not just the first write, seeds the row.
``slug`` and ``name`` derive deterministically from the full UUID
(see :func:`_derive_slug`); a future v0.3 tenant-provisioning API can
rename them out of band (``slug`` carries no FK and the get-or-create
never overwrites an existing row).

The statement is a single dialect-native
``INSERT ... ON CONFLICT DO NOTHING`` with **no named arbiter**, so
``DO NOTHING`` covers every unique index on ``tenant`` — the ``id``
primary key *and* the ``tenant_slug_idx`` unique index. ``ON CONFLICT
DO NOTHING`` makes concurrent first-writes safe: N requests racing on
the same fresh ``tenant_id`` all attempt the insert, exactly one row
lands, the losers no-op without raising. No ``SELECT``-then-``INSERT``
race window, no advisory lock, no application-level dedupe.

Naming only ``id`` as the arbiter (the original shape) was **not**
race-safe: PostgreSQL raised a ``unique_violation`` on the non-arbiter
``tenant_slug_idx`` when concurrent transactions inserted the same
``(id, slug)`` row, because that index's conflict was checked outside
the ``id`` arbiter's speculative-insertion wait path. Dropping the
``index_elements`` so the arbiter is *all* unique indexes closes that
window (#983). The slug is bijective with the ``id`` (see
:func:`_derive_slug`), so a slug conflict always implies the same
``id`` row already exists — arbitrating against both indexes is exactly
the intended idempotent no-op, never a wrong suppression.

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

    ``tenant-<full-uuid>`` — the canonical hyphenated UUID form, so
    the slug is bijective with the ``id`` primary key. ``tenant.slug``
    has its own ``UNIQUE`` index (``tenant_slug_idx``) separate from the
    ``id`` primary key. The full-UUID form makes the slug exactly as
    unique as the ``id``: a slug conflict can only arise for a row that
    already shares the ``id``, never for two genuinely-distinct tenants.
    That bijection is what lets :func:`ensure_tenant` arbitrate
    ``ON CONFLICT DO NOTHING`` against *both* unique indexes safely (a
    slug conflict ⟺ an ``id`` conflict, so DO-NOTHING on either is the
    same idempotent no-op). Truncating to a prefix would break the
    bijection — two distinct tenant UUIDs sharing the prefix would share
    a slug and raise an ``IntegrityError`` on the slug index, 500-ing
    every authenticated request for the colliding tenant forever
    (``ensure_tenant`` runs on every authenticated request). Still
    deterministic (re-deriving for the same ``tenant_id`` is stable) and
    namespaced under ``tenant-`` so a future v0.3 provisioning API can
    tell auto-seeded rows from operator-named ones at a glance. The slug
    carries no FK, so a later rename is a single ``UPDATE`` with no
    cascade.
    """
    return f"tenant-{tenant_id}"


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
    #
    # No ``index_elements`` is passed, so the ``DO NOTHING`` arbiter is
    # *every* unique index on ``tenant`` — the ``id`` primary key **and**
    # the separate ``tenant_slug_idx`` unique index. Naming only ``id``
    # as the arbiter (the previous shape) does **not** suppress a
    # violation on a non-arbiter unique index: PostgreSQL raises a
    # ``unique_violation`` on ``tenant_slug_idx`` when concurrent
    # transactions insert the *same* ``(id, slug)`` row, because the
    # slug-index conflict is checked outside the ``id`` arbiter's
    # speculative-insertion wait path and surfaces before the winner's
    # row is visible (PG docs, INSERT ... ON CONFLICT — a named
    # ``conflict_target`` only handles conflicts on that target;
    # ``DO NOTHING`` carries weaker concurrency guarantees than
    # ``DO UPDATE``). Since ``_derive_slug`` makes the slug bijective
    # with the ``id``, a slug conflict always implies the same-``id``
    # row already exists (or is mid-insert by a concurrent winner), so
    # arbitrating against both indexes is exactly the intended
    # idempotent no-op — and it is the form that survives the
    # concurrent same-tenant first-write race (#983).
    stmt: Insert
    if conn.dialect.name == "postgresql":
        stmt = pg_insert(Tenant).values(**values).on_conflict_do_nothing()
    else:
        # SQLite (dev/test via aiosqlite). The bare form arbitrates
        # against both unique indexes here too; SQLite serialises
        # writes so it never exercised the PG race, but keeping the two
        # branches symmetric avoids a dialect-divergent conflict target.
        stmt = sqlite_insert(Tenant).values(**values).on_conflict_do_nothing()

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
