# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keep the entry metadata on each kb revision.

Revision ID: 0140
Revises: 0139
Create Date: 2026-11-07

A kb revision kept the body but not the metadata (the header). A change
to the header alone added no revision. So a revision id did not show
that someone had changed the header.

From now on a change to the body or to the metadata adds a revision,
and the revision keeps the metadata it was written with. One revision
id then names one exact state of the entry.

This migration adds one nullable JSON column, ``entry_metadata``. Rows
that exist already keep ``NULL``: the migration cannot know what the
header was when they were written.

Purely additive. An older image never reads the column, so a rollback
is safe. ``downgrade()`` drops the column and the metadata kept in it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0140"
down_revision: str | None = "0139"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic reads these names itself. List them in __all__, so the code-quality
# check does not report them as unused globals.
__all__ = [
    "branch_labels",
    "depends_on",
    "down_revision",
    "downgrade",
    "revision",
    "upgrade",
]

_TABLE = "kb_revision"
_COLUMN = "entry_metadata"


def upgrade() -> None:
    """Add the nullable ``entry_metadata`` column."""
    json_type = sa.JSON(none_as_null=True).with_variant(
        postgresql.JSONB(none_as_null=True), "postgresql"
    )
    op.add_column(_TABLE, sa.Column(_COLUMN, json_type, nullable=True))


def downgrade() -> None:
    """Drop the ``entry_metadata`` column."""
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_COLUMN)
