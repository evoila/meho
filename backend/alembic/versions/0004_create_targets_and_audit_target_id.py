# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the targets table and add audit_log.target_id column.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-12

This migration is the first schema step of Initiative #224 (G0.3
Targets-as-data). It adds two structural pieces:

* A new ``targets`` table — the central registry of every SSH/API
  endpoint a MEHO agent may reach. Per-tenant (``tenant_id`` NOT NULL,
  soft FK in v0.2 by the same discipline 0002 established for
  ``audit_log.tenant_id``). Key fields: ``name`` (human handle within
  the tenant), ``product`` / ``host`` / ``port`` / ``fqdn``
  (connection coordinates), ``aliases`` (TEXT[] — searchable secondary
  names), ``auth_model`` (default ``shared_service_account``),
  ``vpn_required``, ``extras`` (JSONB escape hatch), ``secret_ref``
  (Vault path, filled in later goals).
* A new nullable ``target_id uuid`` column on ``audit_log`` — same
  soft-FK discipline as ``tenant_id`` in 0002. Every request that
  operates on a specific target will carry the target UUID in the
  audit row; generic requests (health, policy listing) leave it NULL.
  A future tightening migration can backfill and add the FK once
  the G0.3 CRUD layer is stable.

Why no FK clause to ``targets.id`` in v0.2
-------------------------------------------

Identical rationale to ``audit_log.tenant_id`` (see 0002 docstring):
reversibility on a populated table and backfill discipline both favour
keeping the column shape soft until a dedicated tightening migration
can coordinate the FK addition, backfill, and optional NOT NULL flip
in one atomic change.

Indexes
-------

``targets`` carries four indexes:

* ``targets_tenant_name_idx`` — unique b-tree on ``(tenant_id, name)``.
  Enforces the "name is unique per tenant" invariant at the DB layer.
  Named index only; no ``unique=True`` on the column to avoid PG
  auto-generating a duplicate anonymous index alongside it.
* ``targets_tenant_product_idx`` — b-tree on ``(tenant_id, product)``.
  Drives the "list targets by product in tenant" query shape.
* ``targets_aliases_gin_idx`` — GIN index on ``aliases`` (PostgreSQL
  only). Enables ``@>`` / ``&&`` / ``<@`` array-containment queries
  for alias lookups. SQLite has no GIN support and TEXT[] is stored
  as JSON-text there — the GIN creation is wrapped in ``if is_postgres:``
  so the migration stays dialect-portable.
* ``audit_log_target_id_idx`` — b-tree on ``audit_log.target_id`` so
  "all audit rows for target X" queries hit the index.

Dialect-portability decisions
------------------------------

Mirrors the discipline 0001 and 0002 established:

* ``targets.id`` server default — PG gets ``gen_random_uuid()``;
  SQLite relies on the ORM ``default=uuid.uuid4`` Python-side.
* ``targets.created_at`` / ``targets.updated_at`` server defaults —
  PG gets ``now()``; SQLite leaves to the ORM defaults.
* ``targets.extras`` server default — PG gets ``'{}'::jsonb``;
  SQLite stores JSON-text and relies on ORM ``default=dict``.
* ``targets.auth_model`` server default — ``'shared_service_account'``
  on both dialects (text literal works on SQLite too).
* ``targets.vpn_required`` server default — ``false`` on PG,
  ``0`` on SQLite (SQLite stores booleans as integers).
* GIN index — wrapped in ``if is_postgres:``; skipped entirely on
  SQLite (SQLite cannot index a JSON/TEXT[] column with GIN semantics).

Reversibility contract
-----------------------

``downgrade()`` undoes everything ``upgrade()`` created in reverse
order: audit_log target_id index → audit_log target_id column →
targets GIN index (PG only) → targets indexes → targets table.
Explicit index drops keep the inverse symmetric on SQLite (which does
not always cascade indexes on ``drop_column`` / ``drop_table``).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``targets`` table + add ``target_id`` to ``audit_log``."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # ``extras`` column type — JSONB on PostgreSQL (binary JSON,
    # indexable by ``@>`` / GIN), generic JSON (text) on SQLite.
    # Same portable pattern as ``audit_log.payload`` in 0001.
    extras_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

    # ``aliases`` column type — native TEXT[] on PostgreSQL (supports
    # GIN-indexed containment queries), JSON-text array on SQLite
    # (no native ARRAY type; the GIN index is also skipped there).
    aliases_type = sa.JSON().with_variant(postgresql.ARRAY(sa.Text()), "postgresql")

    op.create_table(
        "targets",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        # ``tenant_id`` — NOT NULL, no FK clause (see module docstring).
        # Unlike ``audit_log.tenant_id`` (which is nullable for chassis-
        # era rows), every target row belongs to exactly one tenant.
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        # ``aliases`` — NOT NULL, empty array default. TEXT[] on PG,
        # JSON array on SQLite. Never NULL: [] means "no aliases".
        sa.Column(
            "aliases",
            aliases_type,
            nullable=False,
            server_default=(sa.text("'{}'::text[]") if is_postgres else sa.text("'[]'")),
        ),
        sa.Column("product", sa.Text(), nullable=False),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("port", sa.Integer(), nullable=True),
        sa.Column("fqdn", sa.Text(), nullable=True),
        sa.Column("secret_ref", sa.Text(), nullable=True),
        sa.Column(
            "auth_model",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'shared_service_account'"),
        ),
        sa.Column(
            "vpn_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false") if is_postgres else sa.text("0"),
        ),
        sa.Column(
            "extras",
            extras_type,
            nullable=False,
            server_default=sa.text("'{}'::jsonb") if is_postgres else sa.text("'{}'"),
        ),
        sa.Column("notes", sa.Text(), nullable=True),
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
        # CHECK constraint ensuring only known AuthModel values can be stored.
        # Extend the IN(...) list when AuthModel gains new enum members.
        sa.CheckConstraint(
            "auth_model IN ('impersonation', 'shared_service_account', 'per_user')",
            name="ck_targets_auth_model",
        ),
    )

    # Named **unique** b-tree on ``(tenant_id, name)`` — enforces the
    # one-name-per-tenant invariant. Uniqueness is on the index, not the
    # column pair, so PG doesn't auto-create a duplicate anonymous index.
    op.create_index(
        "targets_tenant_name_idx",
        "targets",
        ["tenant_id", "name"],
        unique=True,
        postgresql_using="btree",
    )
    # b-tree on ``(tenant_id, product)`` — drives "list targets by
    # product in tenant" query shape.
    op.create_index(
        "targets_tenant_product_idx",
        "targets",
        ["tenant_id", "product"],
        postgresql_using="btree",
    )
    # GIN index on ``aliases`` — PostgreSQL only. SQLite has no GIN
    # support and cannot index a JSON/TEXT[] column with GIN semantics;
    # skipping on non-PG dialects keeps the migration dialect-portable.
    if is_postgres:
        op.create_index(
            "targets_aliases_gin_idx",
            "targets",
            ["aliases"],
            postgresql_using="gin",
        )

    # ``audit_log.target_id`` — nullable by design (see module
    # docstring). No FK to ``targets.id`` in v0.2; v0.2.next tightens.
    op.add_column(
        "audit_log",
        sa.Column("target_id", sa.Uuid(), nullable=True),
    )
    op.create_index(
        "audit_log_target_id_idx",
        "audit_log",
        ["target_id"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    """Reverse the upgrade — drop everything in reverse order.

    Symmetric inverse of :func:`upgrade`. Indexes are dropped
    explicitly so the migration is reversible cleanly on SQLite
    (which does not always cascade indexes on ``drop_column`` /
    ``drop_table``) as well as PostgreSQL.
    """
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    op.drop_index("audit_log_target_id_idx", table_name="audit_log")
    op.drop_column("audit_log", "target_id")

    if is_postgres:
        op.drop_index("targets_aliases_gin_idx", table_name="targets")

    op.drop_index("targets_tenant_product_idx", table_name="targets")
    op.drop_index("targets_tenant_name_idx", table_name="targets")
    op.drop_table("targets")
