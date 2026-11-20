# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``approval_request.session_from_client``.

Revision ID: 0179
Revises: 0178
Create Date: 2026-11-16

One nullable boolean column. ``True`` when ``agent_session_id`` holds the
login session id the ``meho`` CLI sent, not an agent run or an MCP session.
When MEHO runs the approved operation, it puts that id only on the audit
rows, so the run is not treated as an agent session. Rows that exist
already stay ``NULL``, which MEHO reads like ``False``.

Only adds a column. An older image never reads it, so a rollback is
safe. ``downgrade()`` drops it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0179"
down_revision: str | None = "0178"
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
    """Add the column."""
    op.add_column(
        "approval_request",
        sa.Column("session_from_client", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    """Drop the column."""
    op.drop_column("approval_request", "session_from_client")
