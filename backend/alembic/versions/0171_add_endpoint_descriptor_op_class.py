# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add the stored op class to ``endpoint_descriptor`` (#6485).

Revision ID: 0171
Revises: 0170
Create Date: 2026-11-15

One nullable column, ``endpoint_descriptor.op_class``: the class a
connector's safety floor gave to an ingested operation, for example
``credential_read``. ``NULL`` means no floor gave a class, and the class
comes from the op id as before. Existing rows start as ``NULL``. An older
build never reads the column.

``downgrade()`` drops the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0171"
down_revision: str | None = "0170"
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


def upgrade() -> None:
    """Add the op class column."""
    op.add_column("endpoint_descriptor", sa.Column("op_class", sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the op class column."""
    op.drop_column("endpoint_descriptor", "op_class")
