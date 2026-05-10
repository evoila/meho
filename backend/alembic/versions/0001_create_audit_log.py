# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the audit_log table.

Revision ID: 0001
Revises:
Create Date: 2026-05-10

This is the first migration on the schema, landing the v0.1 audit-log
shape (Initiative #26, Task #28). Every authenticated request writes
one row into this table synchronously, *before* the response yields
back to the ASGI send chain — see :mod:`meho_backplane.audit`.

The table is intentionally simple in v0.1: operator, what they did,
when, the result. Goal-shaped fields ("approval state", "policy
decision", "tenant") are out of scope (they belong to the policy +
topology Goals post-Goal-2). The ``payload jsonb`` column is the
forward-compat escape hatch for future structured fields without
schema changes.

Dialect-portability decisions
-----------------------------

The migration runs cleanly against both PostgreSQL (production) and
SQLite (dev/test via aiosqlite — the pattern the existing
``test_alembic_upgrade_head_against_sqlite`` test exercises).

* ``id`` server default — PostgreSQL 13+ ships ``gen_random_uuid()``
  built-in; SQLite has no equivalent. The PG branch attaches the
  server default; the SQLite branch leaves the column without a
  default and lets the ORM model's ``default=uuid.uuid4`` fill it
  Python-side. Either way the column is NOT NULL and always
  populated by the time the audit middleware commits.
* ``occurred_at`` server default — same shape: PG gets ``now()``;
  SQLite leaves it to the ORM ``default=lambda: datetime.now(UTC)``.
  In practice the audit middleware always sets ``occurred_at``
  explicitly, so the server default only fires on hypothetical
  manual ``INSERT`` paths (e.g. operations engineers replaying lost
  rows from logs).
* ``payload jsonb`` server default — PG gets ``'{}'::jsonb``;
  SQLite stores the column as JSON-text and the ORM ``default=dict``
  fills it. The functional contract ("never NULL") is preserved
  across both.
* Indexes — both indexes use b-tree explicitly on PG via the
  ``postgresql_using="btree"`` kwarg; on SQLite the kwarg is a
  no-op (SQLite only has b-tree indexes).

Downgrade drops the table; this is the very first migration on the
schema, so dropping the audit_log table is the only revertible
operation. Initiative #26's "backward-compat-only migration
discipline" applies to *subsequent* migrations — once production
data exists, ``DROP TABLE audit_log`` is no longer reversible
without restoring from backup. The CI guard landing in Task #29
will reject ``DROP TABLE`` patterns on every migration *after*
this one.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``audit_log`` table plus its two btree indexes."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # The ``UUID`` columns use SQLAlchemy's portable :class:`Uuid`
    # type, which compiles to ``UUID`` on PostgreSQL and ``CHAR(32)``
    # on SQLite — the dev/test path stays drivable without changing
    # the production schema. The ``payload`` column compiles to PG
    # ``JSONB`` (true binary JSON, indexable by ``@>`` etc.) via the
    # ``with_variant`` override; on SQLite it falls back to the
    # generic :class:`JSON` type which stores text.
    payload_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "audit_log",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
        sa.Column("operator_sub", sa.Text(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=True),
        sa.Column("duration_ms", sa.Numeric(10, 2), nullable=True),
        sa.Column(
            "payload",
            payload_type,
            nullable=False,
            server_default=sa.text("'{}'::jsonb") if is_postgres else sa.text("'{}'"),
        ),
    )
    op.create_index(
        "audit_log_occurred_at_idx",
        "audit_log",
        ["occurred_at"],
        postgresql_using="btree",
    )
    op.create_index(
        "audit_log_operator_sub_idx",
        "audit_log",
        ["operator_sub"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    """Drop the ``audit_log`` table and both indexes.

    Indexes are dropped explicitly even though ``op.drop_table``
    cascades them in PG; the explicit drops keep the migration's
    inverse symmetric and survive dialect drift.
    """
    op.drop_index("audit_log_operator_sub_idx", table_name="audit_log")
    op.drop_index("audit_log_occurred_at_idx", table_name="audit_log")
    op.drop_table("audit_log")
