# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add the operation catalog state table (#5019).

Revision ID: 0119
Revises: 0118
Create Date: 2026-10-27

One additive table, ``operation_catalog_state``: exactly one row
(``id = 1``) that names the release whose start last wrote the built-in
operation rows, and when. MEHO writes the row in the same transaction as
the operation rows, so the row and the operation rows always change
together. A database from before this migration has no row until the
first start of this release. An older build never reads the table.

``downgrade()`` drops the table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0119"
down_revision: str | None = "0118"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "operation_catalog_state"


def upgrade() -> None:
    """Create the operation catalog state table."""
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("written_by_release", sa.Text(), nullable=False),
        sa.Column("written_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_operation_catalog_state_single_row"),
    )


def downgrade() -> None:
    """Drop the operation catalog state table."""
    op.drop_table(_TABLE)
