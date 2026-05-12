# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the tenant table and add audit_log.tenant_id.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-12

This is the first schema-level step of Initiative #222 (G0.1 Tenant
model). The migration adds two structural pieces that every later
G3-G9 Goal depends on:

* A new ``tenant`` table (id UUID PK, slug TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL, created_at timestamptz NOT NULL DEFAULT now()).
  ``slug`` is what operators see (``rdc-internal``, ``customer-a``);
  ``id`` is the FK keystone every per-tenant feature will join on.
* A new ``tenant_id uuid`` column on ``audit_log``, **nullable in
  v0.2 by design**. Every authenticated request post-G0.1-T3 will
  populate it via the contextvar binding; chassis-era audit rows
  stay readable with ``tenant_id IS NULL``. A future v0.2.next
  tightening migration can backfill once ``rdc-internal`` is the
  canonical post-migration tenant and then flip the column to
  NOT NULL — making it NOT NULL now would break the chassis →
  v0.2 upgrade path on the consumer's live deployment.

Why no FK clause to ``tenant.id`` in v0.2
-----------------------------------------

The ``tenant_id`` column ships **without** a ``REFERENCES tenant``
clause. Two reasons:

1. **Reversibility on a populated table.** Adding a FK to an
   existing column on a table that already has rows would force the
   downgrade to either drop the constraint cleanly (fine on PG, but
   noisy) *or* leave dangling rows referencing a soon-to-be-dropped
   ``tenant`` table. Keeping the column shape soft in v0.2 means
   ``downgrade()`` is a single ``drop_column`` + ``drop_table``
   pair with no constraint juggling.
2. **Backfill discipline.** v0.2.next will introduce a tightening
   migration that (a) backfills chassis-era rows with the
   ``rdc-internal`` tenant id, (b) flips the column to NOT NULL,
   and (c) attaches the FK. That migration is a single coordinated
   change; introducing the FK now would split the discipline across
   two migrations and require either fragile ordering or a CASCADE
   policy decision before we have data to inform it.

Dialect-portability decisions
-----------------------------

Mirrors the discipline ``0001_create_audit_log.py`` established:

* ``tenant.id`` server default — PG gets ``gen_random_uuid()``;
  SQLite (dev/test via aiosqlite) leaves the column without a
  server default and relies on the ORM ``default=uuid.uuid4``
  Python-side at insert time. Either way the column is NOT NULL
  and always populated by the time a row commits.
* ``tenant.created_at`` server default — PG gets ``now()``;
  SQLite leaves it to the ORM ``default=lambda: datetime.now(UTC)``.
  In practice the seeding migration (G7.1, future) will set
  ``created_at`` explicitly; the server default is the safety net
  for ad-hoc PG inserts.
* ``audit_log.tenant_id`` is nullable on **every** dialect — no
  server default needed.
* Indexes — both new indexes use b-tree explicitly on PG via
  ``postgresql_using="btree"``; on SQLite the kwarg is a no-op
  (SQLite only has b-tree indexes). The unique constraint on
  ``slug`` is enforced exclusively by the named
  ``tenant_slug_idx`` (declared ``unique=True``); the column
  itself omits ``unique=True`` to avoid PG also auto-creating a
  second, redundantly-named unique index for the same column.

Reversibility contract
----------------------

``downgrade()`` undoes everything ``upgrade()`` created, in reverse
order: drop the audit_log index → drop the audit_log column → drop
the tenant indexes → drop the tenant table. Indexes are dropped
explicitly even though ``op.drop_table``/``op.drop_column`` cascade
on PG; the explicit drops keep the inverse symmetric and survive
dialect drift (SQLite, in particular, does not always cascade
indexes on column drop). The CI guard
(``scripts/ci/check_migration_compat.py``) inspects only
``upgrade()``, so the destructive ops in ``downgrade()`` are
allowed by design — see that script's docstring.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``tenant`` table + add ``tenant_id`` to ``audit_log``."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # ``tenant.id`` — UUID PK. Same portable :class:`Uuid` shape the
    # ``audit_log`` migration uses: compiles to ``UUID`` on PostgreSQL,
    # ``CHAR(32)`` on SQLite. PG gets the server-side default so
    # operator-side inserts (seeding scripts, future tenants-CRUD UX)
    # don't have to mint a uuid client-side.
    op.create_table(
        "tenant",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        # ``slug`` uniqueness is enforced by the named ``tenant_slug_idx``
        # below (declared ``unique=True``). We deliberately do **not** set
        # ``unique=True`` on the column itself — PostgreSQL would otherwise
        # auto-generate a *second* unique b-tree index alongside our named
        # one, doubling per-write index maintenance for zero benefit. One
        # named unique b-tree is enough; ``Index(..., unique=True)`` on the
        # model side keeps autogenerate diffs clean.
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
    )
    # Named **unique** b-tree index on ``slug`` — single source of
    # uniqueness enforcement for this column. We intentionally do not
    # set ``unique=True`` on the ``slug`` column above: PG would
    # auto-generate an additional unique index there with an
    # opaque dialect-specific name, leaving us with two structurally
    # identical indexes maintained on every insert/update of
    # ``tenant``. The named ``tenant_slug_idx`` here is the stable
    # identifier later migrations and operators reference; the
    # ``unique=True`` kwarg makes it the constraint enforcer too. The
    # index spec mirrors ``Index("tenant_slug_idx", "slug",
    # unique=True, postgresql_using="btree")`` declared on the
    # SQLAlchemy model so autogenerate stays a no-op.
    op.create_index(
        "tenant_slug_idx",
        "tenant",
        ["slug"],
        unique=True,
        postgresql_using="btree",
    )

    # ``audit_log.tenant_id`` — nullable on purpose (see module
    # docstring). No FK to ``tenant.id`` in v0.2; v0.2.next tightens.
    op.add_column(
        "audit_log",
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
    )
    op.create_index(
        "audit_log_tenant_id_idx",
        "audit_log",
        ["tenant_id"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    """Reverse the upgrade — drop everything in reverse order.

    Symmetric inverse of :func:`upgrade`. Indexes are dropped
    explicitly so the migration is reversible cleanly on SQLite
    (which does not always cascade indexes on ``drop_column`` /
    ``drop_table``) as well as PostgreSQL.
    """
    op.drop_index("audit_log_tenant_id_idx", table_name="audit_log")
    op.drop_column("audit_log", "tenant_id")
    op.drop_index("tenant_slug_idx", table_name="tenant")
    op.drop_table("tenant")
