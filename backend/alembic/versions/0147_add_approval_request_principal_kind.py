# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Save the kind of the requester on each approval request.

Revision ID: 0147
Revises: 0146
Create Date: 2026-11-08

An approval request saved who asked (``principal_sub``) and their name
(``principal_name``), but not whether a person or a machine asked. So the
console and the CLI could not show that an agent asked (#3885).

This migration adds one nullable text column, ``principal_kind``. It
holds the requester's principal kind: ``user``, ``agent``, ``service`` or
``runner``. Rows that exist already keep ``NULL``: the migration cannot
know who made them. The surfaces then show no marker, as before.

Purely additive. An older image never reads the column, so a rollback
is safe. ``downgrade()`` drops the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0147"
down_revision: str | None = "0146"
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

_TABLE = "approval_request"
_COLUMN = "principal_kind"


def upgrade() -> None:
    """Add the nullable ``principal_kind`` column."""
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the ``principal_kind`` column."""
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_COLUMN)
