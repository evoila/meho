# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``audit_log.parent_audit_id`` for composite-operation audit-tree linkage.

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-14

This migration is the schema piece of Task #398 (G0.6-T7 composite-
operation recursion infrastructure) under Initiative #388. The G0.6
dispatcher (#396) writes one ``audit_log`` row per ``dispatch()`` call;
composite handlers (``source_kind='composite'``) call ``dispatch_child``
inside their body, and each child call writes its own audit row. T7
adds the structural link between parent + child rows so audit-tree
queries (``query_audit({shape: "tree"})``, G8.1 / G8.2) can reconstruct
the full operation tree -- composite parent → N children → their
post-dispatch rows -- via a single recursive CTE.

What this migration adds
------------------------

* ``audit_log.parent_audit_id uuid`` — nullable. Points at the
  ``audit_log.id`` of the composite operation whose handler issued the
  recursive ``dispatch_child(...)`` call that produced this row. Top-
  level dispatches (the operator-facing entry point, not a nested
  sub-op) leave the column NULL.
* ``audit_log_parent_audit_id_idx`` — b-tree index on the new column.
  Drives the recursive-CTE traversal at audit-replay time.

Why no foreign key clause in v0.2
---------------------------------

Identical rationale to ``audit_log.tenant_id`` (0002) and
``audit_log.target_id`` (0004): keeping the column shape *soft* (no FK
clause, no NOT NULL) makes the migration trivially reversible on a
populated table and defers backfill/tightening to a dedicated future
migration. Self-referential FKs on append-only audit tables are
particularly painful to retrofit -- the canonical PostgreSQL practice
is to ship the column nullable + indexed, then add the FK in a separate
``ALTER TABLE ... ADD CONSTRAINT ... NOT VALID`` + ``VALIDATE
CONSTRAINT`` cycle once the chassis-era backfill plan is settled.

Dialect-portability decisions
------------------------------

Mirrors the same discipline 0001 / 0002 / 0004 established: pure DDL,
no server defaults (Python-side ORM default of ``None`` works on both
PG and SQLite), and a named index that is dropped explicitly in
``downgrade()`` so the inverse works cleanly on both dialects.

Reversibility contract
-----------------------

``downgrade()`` undoes everything ``upgrade()`` created in reverse
order: drop the parent_audit_id index → drop the parent_audit_id
column. SQLite-portable because every step uses generic SQLAlchemy DDL
the dialect understands without quoting tricks.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``audit_log.parent_audit_id`` column + named index."""
    op.add_column(
        "audit_log",
        sa.Column("parent_audit_id", sa.Uuid(), nullable=True),
    )
    op.create_index(
        "audit_log_parent_audit_id_idx",
        "audit_log",
        ["parent_audit_id"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    """Reverse the upgrade -- drop the index and column."""
    op.drop_index("audit_log_parent_audit_id_idx", table_name="audit_log")
    op.drop_column("audit_log", "parent_audit_id")
