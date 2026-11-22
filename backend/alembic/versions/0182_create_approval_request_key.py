# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create ``approval_request_key``: more retry keys of one approval request.

Revision ID: 0182
Revises: 0181
Create Date: 2026-11-22

With ``APPROVAL_SAME_CALL_REUSE`` on, a call with retry key B can get the
waiting request of the same call that was made with key A. The request row
holds only key A. This table keeps key B, so a later retry with key B gets
the same request, also after it was approved.
``docs/codebase/approvals.md``.

Reversibility
-------------

Purely additive. An older image never reads the table, so a rollback is
safe. ``downgrade()`` drops the table; a retry with such a key B then makes
a new request.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0182"
down_revision: str | None = "0181"
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

_TABLE = "approval_request_key"
_INDEX = "approval_request_key_key_idx"


def upgrade() -> None:
    """Create the table and its lookup index."""
    op.create_table(
        _TABLE,
        sa.Column("approval_request_id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("idempotency_key", sa.Text(), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(_INDEX, _TABLE, ["idempotency_key"])


def downgrade() -> None:
    """Drop the table."""
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
