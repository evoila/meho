# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keep the console's approval sign-in on ``web_session``.

Revision ID: 0176
Revises: 0175
Create Date: 2026-11-16

Why
---

An approval in the console needs the same sign-in level as one on the
command line (#6282). The console signs the person in once more with its
own approval client, and keeps the token of that sign-in with the session
until the next approve.

* ``approval_token`` -- the access token of the console approval sign-in,
  Fernet-encrypted like ``access_token``. ``NULL`` when the session has no
  approval sign-in. A new approval sign-in replaces it.

The column is nullable and has no default, so the add is safe on a filled
table and needs no backfill. An older build never reads it.

Reversibility
-------------

``downgrade()`` drops the column. A console approval then needs a new
approval sign-in after the next upgrade.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0176"
down_revision: str | None = "0175"
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
    """Add the approval sign-in column to ``web_session``."""
    op.add_column("web_session", sa.Column("approval_token", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    """Drop the approval sign-in column."""
    op.drop_column("web_session", "approval_token")
