# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create ``agent_session_link`` and ``agent_session_audit_link``: links of uploaded sessions.

Revision ID: 0181
Revises: 0180
Create Date: 2026-11-21

When a person uploads an agent session, MEHO finds the audit rows of the
session's MCP calls and stores their MCP session id here, one row per MCP
session. An audit row with that ``agent_session_id`` then links to the
stored session. A ``meho`` CLI command carries the login session id, which
many agent sessions share, so MEHO stores the id of each of its audit rows
in ``agent_session_audit_link``. ``docs/codebase/session-store.md``.

Reversibility
-------------

Purely additive. An older image never reads the table, so a rollback is
safe. ``downgrade()`` drops the tables; audit rows then show no link.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0181"
down_revision: str | None = "0180"
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

_TABLE = "agent_session_link"
_INDEX = "agent_session_link_tenant_mcp_session_idx"
_AUDIT_TABLE = "agent_session_audit_link"
_AUDIT_INDEX = "agent_session_audit_link_tenant_audit_idx"


def upgrade() -> None:
    """Create the two link tables and their lookup indexes."""
    op.create_table(
        _TABLE,
        sa.Column(
            "record_id",
            sa.Uuid(),
            sa.ForeignKey("agent_session_record.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("mcp_session_id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("work_ref", sa.Text(), nullable=True),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(_INDEX, _TABLE, ["tenant_id", "mcp_session_id"])
    op.create_table(
        _AUDIT_TABLE,
        sa.Column(
            "record_id",
            sa.Uuid(),
            sa.ForeignKey("agent_session_record.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("audit_id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(_AUDIT_INDEX, _AUDIT_TABLE, ["tenant_id", "audit_id"])


def downgrade() -> None:
    """Drop the two link tables."""
    op.drop_index(_AUDIT_INDEX, table_name=_AUDIT_TABLE)
    op.drop_table(_AUDIT_TABLE)
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
