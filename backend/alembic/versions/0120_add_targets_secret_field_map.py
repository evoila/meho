# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``targets.secret_field_map`` (#3711).

Revision ID: 0120
Revises: 0119
Create Date: 2026-10-28

One nullable JSON column. It maps the field name a connector reads to the
field name the target's secret really uses, for example
``{"ssh_private_key": "private_key"}``. It holds field names only, never a
credential value. ``NULL`` means no map, so existing rows keep working as
before and no backfill is needed. An older build never reads the column.

``downgrade()`` drops the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0120"
down_revision: str | None = "0119"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``secret_field_map`` column to ``targets``."""
    op.add_column(
        "targets",
        sa.Column(
            "secret_field_map",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Drop the ``secret_field_map`` column."""
    op.drop_column("targets", "secret_field_map")
