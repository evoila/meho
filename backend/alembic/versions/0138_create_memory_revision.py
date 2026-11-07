# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the ``memory_revision`` table.

Revision ID: 0138
Revises: 0137
Create Date: 2026-11-07

A memory entry is one ``documents`` row (``source='memory'``). A new write
overwrites that row, so the old body and metadata were lost. This table
keeps each version a memory write stores. The ``documents`` row stays the
latest head and nothing about it changes.

What this migration adds
------------------------

* The ``memory_revision`` table, with a foreign key to ``documents.id``
  (``ON DELETE CASCADE``) and to ``tenant.id``.
* A unique index ``memory_revision_entry_number_idx`` on
  ``(entry_id, revision_number)``.

No backfill: there is no stored history to copy. An existing entry gets
its first revision at its next write.

Reversibility
-------------

Purely additive. An older image never reads the table, so a rollback is
safe. ``downgrade()`` drops the table, which discards the revisions; the
``documents`` rows are untouched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0138"
down_revision: str | None = "0137"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic reads these names itself. List them in __all__, so the code-quality
# check does not report them as unused globals. Keep a plain list of strings
# with no type annotation: the check reads only that form.
__all__ = [
    "branch_labels",
    "depends_on",
    "down_revision",
    "downgrade",
    "revision",
    "upgrade",
]

_TABLE = "memory_revision"


def upgrade() -> None:
    """Create ``memory_revision`` and its unique index."""
    is_postgres = op.get_bind().dialect.name == "postgresql"
    op.create_table(
        _TABLE,
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column(
            "entry_id",
            sa.Uuid(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "metadata",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("written_by_sub", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "memory_revision_entry_number_idx",
        _TABLE,
        ["entry_id", "revision_number"],
        unique=True,
    )


def downgrade() -> None:
    """Drop ``memory_revision``; the revisions are lost, the entries stay."""
    op.drop_index("memory_revision_entry_number_idx", table_name=_TABLE)
    op.drop_table(_TABLE)
