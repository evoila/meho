# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add the owner package to ``endpoint_descriptor`` (#5037).

Revision ID: 0120
Revises: 0119
Create Date: 2026-10-28

One nullable column, ``endpoint_descriptor.owner_package``: the connector
package that wrote the row at start. ``NULL`` means the op is built into
MEHO. Existing rows start as ``NULL``; the next start fills it for the
ops of an installed package. An older build never reads the column.

``downgrade()`` drops the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0120"
down_revision: str | None = "0119"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the owner package column."""
    op.add_column("endpoint_descriptor", sa.Column("owner_package", sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the owner package column."""
    op.drop_column("endpoint_descriptor", "owner_package")
