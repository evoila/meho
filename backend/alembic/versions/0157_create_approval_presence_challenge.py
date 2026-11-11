# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create ``approval_presence_challenge``.

Revision ID: 0157
Revises: 0156
Create Date: 2026-11-11

One row per started passkey touch for an approval. The row names exactly
what the approval will run (the binding hash), can be used once, and ends
after a short time. Nothing writes the table while the
``APPROVAL_PRESENCE_TOUCH`` switch is off.

The table is new and empty, so the migration changes no behaviour on its
own. ``downgrade()`` drops the index and the table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0157"
down_revision: str | None = "0156"
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

_TABLE = "approval_presence_challenge"


def upgrade() -> None:
    """Create the table and its clean-up index."""
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("approver_sub", sa.Text(), nullable=False),
        sa.Column("request_ids", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("binding_hash", sa.Text(), nullable=False),
        sa.Column("server_random", sa.Text(), nullable=False),
        sa.Column("card_hash", sa.Text(), nullable=False),
        sa.Column("match_code", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("session_kind", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "channel IN ('web', 'cli_link', 'rest_link')",
            name="ck_approval_presence_challenge_channel",
        ),
    )
    op.create_index("approval_presence_challenge_created_at_idx", _TABLE, ["created_at"])


def downgrade() -> None:
    """Drop the index, then the table."""
    op.drop_index("approval_presence_challenge_created_at_idx", table_name=_TABLE)
    op.drop_table(_TABLE)
