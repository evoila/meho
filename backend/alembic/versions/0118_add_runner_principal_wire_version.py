# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add the runner's last wire version to ``runner_principal`` (#5020).

Revision ID: 0118
Revises: 0117
Create Date: 2026-10-27

One nullable column, ``runner_principal.wire_version``: the wire version
a satellite runner sent on its last call. The runner list shows it, so an
operator can see a runner that runs another version than the central
MEHO. Existing rows start as ``NULL`` and get a value on the runner's next
call. An older build never reads the column.

``downgrade()`` drops the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0118"
down_revision: str | None = "0117"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the wire version column."""
    op.add_column("runner_principal", sa.Column("wire_version", sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the wire version column."""
    op.drop_column("runner_principal", "wire_version")
