# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Clear the approval flags that ``push_subscription`` rows keep.

Revision ID: 0176
Revises: 0175

Each push subscription keeps the person's login facts, read at their
last sign-in (migration ``0161``). MEHO reads the groups
``MEHO-Approvers`` and ``MEHO-SA-Operators`` as Readers and Operators,
so no default group sets the ``approver`` or ``self_approver`` flag.

This migration sets both flags to false on every row. A person whose
login still carries an ``approver`` claim gets the flag back at the
next sign-in, when MEHO reads the login facts again. The other facts
stay as they are.

The migration only changes data. It is safe to run twice.
``downgrade()`` changes nothing: the old flags are not restored.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0176"
down_revision: str | None = "0175"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = [
    "branch_labels",
    "depends_on",
    "down_revision",
    "downgrade",
    "revision",
    "upgrade",
]

_CLEAR: Final[sa.TextClause] = sa.text(
    "UPDATE push_subscription SET approver = :off, self_approver = :off "
    "WHERE approver = :on OR self_approver = :on"
).bindparams(sa.bindparam("off", False), sa.bindparam("on", True))


def upgrade() -> None:
    """Set ``approver`` and ``self_approver`` to false on every row."""
    op.get_bind().execute(_CLEAR)


def downgrade() -> None:
    """Nothing to undo: the old flags are not restored."""
