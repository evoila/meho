# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``client_id`` to ``machine_login``.

Revision ID: 0189
Revises: 0188
Create Date: 2026-11-26

* ``machine_login.client_id`` -- Text, nullable. The name MEHO shows for an
  agent, service or runner login, for example ``agent:<name>``. MEHO fills
  it on the next call of the login. The block list reads it, so a block of
  that name also holds for the login id.
* ``machine_login_client_id_idx`` -- b-tree on ``client_id``.

Existing rows start as ``NULL``. ``downgrade()`` drops the index and the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0189"
down_revision: str | None = "0188"
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


def upgrade() -> None:
    """Add the column and its index."""
    op.add_column("machine_login", sa.Column("client_id", sa.Text(), nullable=True))
    op.create_index("machine_login_client_id_idx", "machine_login", ["client_id"])


def downgrade() -> None:
    """Drop the index and the column."""
    op.drop_index("machine_login_client_id_idx", table_name="machine_login")
    op.drop_column("machine_login", "client_id")
