# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Index ``audit_log`` by person and time.

Revision ID: 0172
Revises: 0171
Create Date: 2026-11-16

Why
---

The "Your tools" section of the account page shows the last use of each
tool. It reads the newest audit rows of the signed-in person. Without
this index that read sorts all rows of the person. With it, the read
walks the index from the newest row and stops after a fixed number of
rows.

* ``audit_log_operator_sub_occurred_at_idx`` -- non-unique b-tree on
  ``(operator_sub, occurred_at)``.

Reversibility
-------------

``downgrade()`` drops the index. No data changes.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0172"
down_revision: str | None = "0171"
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

_TABLE = "audit_log"
_INDEX = "audit_log_operator_sub_occurred_at_idx"


def upgrade() -> None:
    """Create the person-and-time index."""
    op.create_index(_INDEX, _TABLE, ["operator_sub", "occurred_at"])


def downgrade() -> None:
    """Drop the person-and-time index."""
    op.drop_index(_INDEX, table_name=_TABLE)
