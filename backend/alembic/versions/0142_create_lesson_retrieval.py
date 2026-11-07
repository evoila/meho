# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the ``lesson_retrieval`` table.

Revision ID: 0142
Revises: 0141
Create Date: 2026-11-07

One row for each knowledge or memory entry that a search gave to an agent
session. The weekly knowledge upkeep reads these rows to find entries that
were used, and ``meho audit reflex`` counts them for its learning numbers.

What this migration adds
------------------------

* The ``lesson_retrieval`` table. ``entry_id`` has no foreign key on
  purpose: a row stays when its entry is deleted.
* Two indexes: ``(tenant_id, retrieved_at)`` for the report window and
  ``(entry_id)`` for the upkeep.

Reversibility
-------------

Purely additive. An older image never reads the table, so a rollback is
safe. ``downgrade()`` drops the table and its rows.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0142"
down_revision: str | None = "0141"
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

_TABLE = "lesson_retrieval"


def upgrade() -> None:
    """Create ``lesson_retrieval`` and its two indexes."""
    is_postgres = op.get_bind().dialect.name == "postgresql"
    op.create_table(
        _TABLE,
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("store", sa.Text(), nullable=False),
        sa.Column("entry_id", sa.Uuid(), nullable=False),
        sa.Column("entry_tenant_id", sa.Uuid(), nullable=False),
        sa.Column("agent_session_id", sa.Uuid(), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("expired", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("lesson_retrieval_tenant_time_idx", _TABLE, ["tenant_id", "retrieved_at"])
    op.create_index("lesson_retrieval_entry_idx", _TABLE, ["entry_id"])


def downgrade() -> None:
    """Drop ``lesson_retrieval``; the recorded retrievals are lost."""
    op.drop_index("lesson_retrieval_entry_idx", table_name=_TABLE)
    op.drop_index("lesson_retrieval_tenant_time_idx", table_name=_TABLE)
    op.drop_table(_TABLE)
