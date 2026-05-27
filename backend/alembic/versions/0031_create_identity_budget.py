# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the ``identity_budget`` table for G11.5-T5 (C3-a).

Revision ID: 0031
Revises: 0030
Create Date: 2026-05-27

This migration is the schema substrate of Task #1079 (G11.5-T5) under
Initiative #806 (G11.5 Portability + cost). It adds the
``identity_budget`` table -- one row per
``(tenant_id, principal_sub, window_kind, window_start)`` budget bucket,
carrying optional limits (tokens / cost / requests) and consumption
counters that the runtime increments after every successful agent run.

Why one table, not one per window-kind
--------------------------------------

A ``window_kind`` discriminator (``daily`` / ``weekly`` / ``monthly``)
plus a ``window_start`` timestamp keeps the schema flat: a single
bucket row per (principal, window, period) instead of separate
``daily_identity_budget`` / ``weekly_identity_budget`` /
``monthly_identity_budget`` tables. Consumption is applied to **every
active bucket** the run falls under -- the runtime increments all three
buckets for one run, and an enforcement check (C3-b, Task #1080) can
ask "what's remaining in the daily / weekly / monthly bucket for this
principal" with a uniform query shape.

The ``window_start`` is the inclusive lower bound of the bucket
(midnight UTC for daily, Monday-00:00 UTC for ISO-week weekly, the 1st
of the month at 00:00 UTC for monthly). ``window_end`` is the exclusive
upper bound, persisted so an audit reader can render the bucket without
re-deriving the boundary from ``window_start`` + ``window_kind``.

What this migration adds
------------------------

* The ``identity_budget`` table -- per-tenant, per-principal,
  per-window-kind, per-window-start budget rows.
* One unique constraint:
  ``uq_identity_budget_window`` -- on
  ``(tenant_id, principal_sub, window_kind, window_start)``. The row is
  *keyed* by this tuple; a duplicate would feed nondeterministic
  consumption increments (which row do we charge?). This constraint is
  what makes upserts safe (PG ``ON CONFLICT`` + the SQLite tests using
  the same key set).
* One CHECK constraint: ``ck_identity_budget_window_kind`` -- enforces
  the closed ``window_kind IN ('daily', 'weekly', 'monthly')``
  vocabulary at the DB layer. New window-kinds require both a code
  change and a migration, mirroring the
  :attr:`~meho_backplane.db.models.EndpointDescriptor.safety_level`
  CHECK on ``endpoint_descriptor`` (0005) and the
  ``ck_agent_permission_verdict`` CHECK on ``agent_permission``
  (0022).
* One b-tree index:
  ``identity_budget_tenant_principal_idx`` -- on
  ``(tenant_id, principal_sub)``. Drives the dominant query the
  runtime performs after every run: *"find the active buckets for this
  principal in this tenant"*. Adding ``window_kind`` to the index
  would let PG narrow further per query, but the row count per
  principal is bounded by three (one per window-kind per period) so
  the secondary filter is cheap in-memory.

Why a real FK to ``tenant.id``
------------------------------

Identical rationale to ``agent_permission.tenant_id`` (0022) and
``agent_definition.tenant_id`` (0016): a brand-new clean-slate table
with no chassis-era rows and a clean downgrade that drops the whole
table has no backfill or cascade decision to defer. Enforcing the FK
at the DB layer makes the ownership invariant unbreakable -- a
malformed JWT-claim contextvar surfaces as :class:`IntegrityError` at
insert time rather than as a silently-orphaned budget bucket.

Why ``principal_sub`` has no FK
--------------------------------

Same soft-FK discipline as ``agent_permission.principal_sub`` (0022):
the principal can be a human (no row in ``agent_principal``), a service
account, or an agent. The reference is the opaque JWT ``sub`` claim,
already a stable Keycloak-issued identifier. A tightening migration can
add the FK once the chassis settles on a unified ``principal`` table.

Why limits are nullable, consumption is NOT NULL
------------------------------------------------

Limits (``token_limit`` / ``cost_limit`` / ``request_limit``) are
**nullable** so a bucket can carry "no cap on this dimension yet"
without inventing a sentinel value. Enforcement (C3-b, #1080) reads
NULL as "infinite" -- the data model expresses "we know this much"
without lying about the cap. Consumption (``tokens_consumed`` /
``cost_consumed`` / ``requests_consumed``) is **NOT NULL with default
0** so the upsert path can ``INSERT ... VALUES (..., 0, 0, 0)`` and
the increment query can ``UPDATE ... SET tokens_consumed =
tokens_consumed + N`` without a NULL check.

Dialect-portability decisions
-----------------------------

Mirrors the discipline 0001-0030 established:

* ``id`` server default -- PG gets ``gen_random_uuid()``; SQLite leaves
  the column without a server default and relies on the ORM
  ``default=uuid.uuid4`` Python-side.
* ``created_at`` / ``updated_at`` server defaults -- PG gets ``now()``;
  SQLite leaves it to the ORM ``default=lambda: datetime.now(UTC)``.
* Numeric for money + token counts: ``Numeric(20, 6)`` for tokens
  (a large bound that comfortably holds 64-bit token counts as exact
  fixed-point; some providers emit per-window aggregates that exceed
  ``Integer`` range when the prompt cache is hot) and ``Numeric(14,
  6)`` for cost in USD (eight integer digits is enough for "ten
  million dollars per window" while keeping six-digit fractional cents
  for cache-read precision). ``Numeric`` compiles to ``NUMERIC`` on PG
  and ``NUMERIC`` on SQLite (which stores as TEXT but round-trips
  ``Decimal`` exactly).
* CHECK -- ``sa.CheckConstraint`` with a named constraint; portable
  across both dialects.

Reversibility contract
----------------------

``downgrade()`` undoes everything ``upgrade()`` created in reverse
order: drop the index, then the table (which carries the CHECK and
UNIQUE constraints with it). The index is dropped explicitly so the
reversal is clean on SQLite (which does not always cascade indexes on
``DROP TABLE``) as well as PG. The CI guard
(``scripts/ci/check_migration_compat.py``) inspects only ``upgrade()``;
destructive ops in ``downgrade()`` are allowed by design.

Why additive (not amend ``agent_run``)
--------------------------------------

The ``agent_run.cost`` column (0017) already exists and is stamped on
the row at run termination by the runtime. ``identity_budget`` is the
**per-principal aggregate** across many runs in a window -- a
different unit of currency than the per-run cost. Folding the
aggregate onto ``agent_run`` would force every consumption query into
a ``GROUP BY (principal, window)`` scan; the table is the right
shape.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``identity_budget`` + its index + window-kind CHECK."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    op.create_table(
        "identity_budget",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        # Real REFERENCES tenant(id) FK -- see module docstring.
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenant.id"),
            nullable=False,
        ),
        # JWT ``sub`` of the principal whose budget bucket this is.
        # Soft reference (no FK) -- principal can be human / service /
        # agent; see module docstring.
        sa.Column("principal_sub", sa.Text(), nullable=False),
        # Closed vocabulary: ``daily`` / ``weekly`` / ``monthly``.
        sa.Column("window_kind", sa.Text(), nullable=False),
        # Inclusive lower bound (timestamptz) of the window bucket.
        sa.Column(
            "window_start",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        # Exclusive upper bound. Persisted so audit reads do not have
        # to re-derive the boundary from (window_start, window_kind).
        sa.Column(
            "window_end",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        # Limits -- nullable. NULL = "no cap on this dimension".
        sa.Column("token_limit", sa.Numeric(20, 0), nullable=True),
        sa.Column("cost_limit", sa.Numeric(14, 6), nullable=True),
        sa.Column("request_limit", sa.Integer(), nullable=True),
        # Consumption -- NOT NULL with default 0. See module docstring.
        sa.Column(
            "tokens_consumed",
            sa.Numeric(20, 0),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "cost_consumed",
            sa.Numeric(14, 6),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "requests_consumed",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
        # The bucket is keyed by (tenant, principal, window_kind,
        # window_start); a duplicate would feed nondeterministic
        # consumption increments.
        sa.UniqueConstraint(
            "tenant_id",
            "principal_sub",
            "window_kind",
            "window_start",
            name="uq_identity_budget_window",
        ),
        # DB-layer closed-vocabulary check on window_kind.
        sa.CheckConstraint(
            "window_kind IN ('daily', 'weekly', 'monthly')",
            name="ck_identity_budget_window_kind",
        ),
    )

    # b-tree on (tenant_id, principal_sub) -- drives the dominant
    # "find all active buckets for this principal in this tenant"
    # query the runtime issues after every successful run.
    op.create_index(
        "identity_budget_tenant_principal_idx",
        "identity_budget",
        ["tenant_id", "principal_sub"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    """Reverse the upgrade -- drop the index, then the table.

    Symmetric inverse of :func:`upgrade`. The index is dropped
    explicitly so the migration is reversible cleanly on SQLite (which
    does not always cascade indexes on ``DROP TABLE``) as well as
    PostgreSQL.
    """
    op.drop_index(
        "identity_budget_tenant_principal_idx",
        table_name="identity_budget",
    )
    op.drop_table("identity_budget")
