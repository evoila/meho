# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keep the person's display name on ``web_session``.

Revision ID: 0191
Revises: 0190
Create Date: 2026-11-26

Why
---

The console's user menu showed the Keycloak ``sub`` where a person's name
belongs. The renderer could not reach a name: the
session row had none.

* ``display_name`` -- the ``name`` claim of the access token the login
  verified, Fernet-encrypted like ``access_token`` and with the same key.
  ``NULL`` when the token had no ``name`` claim. Written at login only; a
  token refresh keeps it.

The name is already inside ``access_token`` on the same row, so storing it
encrypted under the same key adds nothing a leaked row would not already
show to someone holding the key, and nothing to someone without it.

The column is nullable and has no default, so the add is safe on a filled
table and needs no backfill. A session from before the upgrade has no name
and shows the neutral fallback until the person signs in again.

Reversibility
-------------

``downgrade()`` drops the column. The console then shows the neutral
fallback for everyone until the next upgrade and sign-in.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0191"
down_revision: str | None = "0190"
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


def upgrade() -> None:
    """Add the display-name column to ``web_session``."""
    op.add_column("web_session", sa.Column("display_name", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    """Drop the display-name column."""
    op.drop_column("web_session", "display_name")
