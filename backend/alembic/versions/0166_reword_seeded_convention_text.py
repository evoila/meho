# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Reword the convention text that migration ``0018`` stored.

Revision ID: 0166
Revises: 0165

Two of the convention texts that ``0018`` seeded named internal
things: one named a private repository, one told the naming history of
an internal lab. ``0018`` now stores neutral wording, so a new install
never holds the old texts. This migration fixes the rows of installs
that already ran ``0018``.

It changes a row only when its body is still exactly an old seeded
text. A row that an admin edited is left as it is. The old texts are
matched by their SHA-256 hash, so this file does not repeat them.

The same rule applies to the history rows (``body_before`` and
``body_after``) that hold an old text.

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

_NEW_NAMING_BODY: Final[str] = (
    "Operator-visible identifiers (target names, hostnames, "
    "IPs, ticket titles, lab object names) must never contain "
    "AI-tool names like 'claude', 'gpt', or other model-shaped "
    "tokens. Older names that break this rule are renamed "
    "where possible. New names start clean."
)

_NEW_PRIVATE_BODY: Final[str] = (
    "The `evoila/meho` repo is public. Internal lab "
    "specifics (real hostnames, IPs, customer names, project "
    "codes) must not appear in commits, issues, or public "
    "docs. Generic naming + sanitised examples only. Internal "
    "coordination happens in a private repository or "
    "other private channels."
)

#: SHA-256 of an old seeded body -> the new body that replaces it.
#: Each new body must match what ``0018`` stores now.
_REWORDS: Final[dict[str, str]] = {
    "0594e166039245c1320c330a672286de026ecc202fb4fe3d1febd423f47f349a": _NEW_NAMING_BODY,
    "5efdf918b393c45fe8122a4a28687620d7dd3124f3c5012e1cc97396490c8e8d": _NEW_PRIVATE_BODY,
}

#: One fixed SELECT and UPDATE per text column (no built-up SQL).
_STATEMENTS: Final[tuple[tuple[sa.TextClause, sa.TextClause], ...]] = (
    (
        sa.text("SELECT id, body FROM tenant_conventions"),
        sa.text("UPDATE tenant_conventions SET body = :new WHERE id = :id"),
    ),
    (
        sa.text(
            "SELECT id, body_before FROM tenant_convention_history WHERE body_before IS NOT NULL"
        ),
        sa.text("UPDATE tenant_convention_history SET body_before = :new WHERE id = :id"),
    ),
    (
        sa.text("SELECT id, body_after FROM tenant_convention_history"),
        sa.text("UPDATE tenant_convention_history SET body_after = :new WHERE id = :id"),
    ),
)


def _new_body(body: object) -> str | None:
    """Return the new body when ``body`` is an old seeded text, else ``None``."""
    if not isinstance(body, str):
        return None
    return _REWORDS.get(hashlib.sha256(body.encode()).hexdigest())


def upgrade() -> None:
    """Replace each old seeded body with its new one, where it is unchanged."""
    bind = op.get_bind()
    for select_sql, update_sql in _STATEMENTS:
        for row_id, body in bind.execute(select_sql).all():
            new = _new_body(body)
            if new is not None:
                bind.execute(update_sql, {"new": new, "id": row_id})


def downgrade() -> None:
    """Nothing to undo: the old text is not restored."""
