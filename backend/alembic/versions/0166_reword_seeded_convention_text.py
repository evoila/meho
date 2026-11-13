# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Reword the convention text that migration ``0018`` stored.

Revision ID: 0166
Revises: 0165

One line of the convention text that ``0018`` seeded named a private
repository. ``0018`` now stores neutral wording, so a new install never
holds the old line. This migration fixes the rows of installs that
already ran ``0018``.

It changes a row only when its body is still exactly the old seeded
text. A row that an admin edited is left as it is. The old text is
matched by its SHA-256 hash, so this file does not repeat it.

The same rule applies to the history rows (``body_before`` and
``body_after``) that hold the old text.

The migration only changes data. It is safe to run twice.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0166"
down_revision: str | None = "0165"
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

#: SHA-256 of the old body of the seeded convention.
_OLD_BODY_SHA256: Final[str] = "5efdf918b393c45fe8122a4a28687620d7dd3124f3c5012e1cc97396490c8e8d"

#: The slug of the seeded convention.
_SLUG: Final[str] = "sensitive-lab-specifics-stay-private"

#: The new body. It must match what ``0018`` stores now.
_NEW_BODY: Final[str] = (
    "The `evoila/meho` repo is public. Internal lab "
    "specifics (real hostnames, IPs, customer names, project "
    "codes) must not appear in commits, issues, or public "
    "docs. Generic naming + sanitised examples only. Internal "
    "coordination happens in a private repository or "
    "other private channels."
)


def _is_old(body: object) -> bool:
    if not isinstance(body, str):
        return False
    return hashlib.sha256(body.encode()).hexdigest() == _OLD_BODY_SHA256


def upgrade() -> None:
    """Replace the old seeded body with the new one, where it is unchanged."""
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, body FROM tenant_conventions WHERE slug = :slug"),
        {"slug": _SLUG},
    ).all()
    for row_id, body in rows:
        if _is_old(body):
            bind.execute(
                sa.text("UPDATE tenant_conventions SET body = :new WHERE id = :id"),
                {"new": _NEW_BODY, "id": row_id},
            )
    for column in ("body_before", "body_after"):
        history_rows = bind.execute(
            sa.text(
                f"SELECT id, {column} FROM tenant_convention_history WHERE {column} IS NOT NULL"
            ),
        ).all()
        for row_id, body in history_rows:
            if _is_old(body):
                bind.execute(
                    sa.text(f"UPDATE tenant_convention_history SET {column} = :new WHERE id = :id"),
                    {"new": _NEW_BODY, "id": row_id},
                )


def downgrade() -> None:
    """Nothing to undo: the old text is not restored."""
