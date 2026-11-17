# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Save the MCP tool profile of the session that started a background run.

Revision ID: 0174
Revises: 0173
Create Date: 2026-11-17

A background run of ``call_operation`` now keeps the MCP tool profile of
the session that started it (``mcp_profile``), when that profile has a
connector list. Only a session of the same profile can then read the run
over MCP, the same rule as for a stored result. This migration adds the
nullable column. Rows that exist already keep ``NULL``: they were started
without such a profile.

An older image never reads the column, so a rollback is safe.
``downgrade()`` drops it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0174"
down_revision: str | None = "0173"
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
    """Add the nullable ``mcp_profile`` column."""
    op.add_column("operation_run", sa.Column("mcp_profile", sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the column."""
    op.drop_column("operation_run", "mcp_profile")
