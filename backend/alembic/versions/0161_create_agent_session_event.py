# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create ``agent_session_event``: the saved events of a hosted agent run.

Revision ID: 0161
Revises: 0160
Create Date: 2026-11-12

A hosted agent run (a run that MEHO starts itself) gets one session in the
agent session store. Its ``agent_session_record`` row has the run id as its
id and as its ``session_id``. Each event of the run (a model turn, a tool
call, a tool result, the final answer or the error) is one row of this
table, in order, saved when it happens. ``docs/codebase/session-store.md``.

The rows hold the event after the secret check of the session store.

Reversibility
-------------

Purely additive. An older image never reads the table, so a rollback is
safe. ``downgrade()`` drops the table and the saved events.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0161"
down_revision: str | None = "0160"
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

_TABLE = "agent_session_event"


def upgrade() -> None:
    """Create the event table."""
    op.create_table(
        _TABLE,
        sa.Column(
            "record_id",
            sa.Uuid(),
            sa.ForeignKey("agent_session_record.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("data", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("quarantined", sa.Boolean(), nullable=False),
        sa.Column("redactions", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('turn', 'tool_call', 'tool_result', 'final', 'error')",
            name="ck_agent_session_event_kind",
        ),
    )


def downgrade() -> None:
    """Drop the event table; the saved events are lost."""
    op.drop_table(_TABLE)
