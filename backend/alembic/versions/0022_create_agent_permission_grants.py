# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``expires_at`` to ``agent_permission`` for G11.2-T6 grant elevation.

Revision ID: 0022
Revises: 0019
Create Date: 2026-05-25

This migration is the schema substrate of Task #819 (G11.2-T6) under
Initiative #803 (the P3 agent identity + RBAC + approval gate). It
extends the ``agent_permission`` table — created by migration ``0019``
(G11.2-T3, #820) — with a nullable ``expires_at`` timestamp column and
a companion index that drives the elevation-expiry sweeper tick.

What this migration adds
------------------------

* ``expires_at`` — nullable ``timestamptz`` (PG) / ``DateTime``
  (SQLite) column on the existing ``agent_permission`` table.
  ``NULL`` = permanent grant. Non-null = time-bounded elevation: the
  grant-expiry sweeper deletes rows past this timestamp, reverting the
  agent to its baseline permissions.
* Index: ``agent_permission_expires_at_idx`` — b-tree on
  ``(expires_at)`` driving the elevation-expiry sweeper tick
  (``WHERE expires_at IS NOT NULL AND expires_at < now()``).

Design decisions
----------------

**Why ``expires_at`` is in the base table** (not a separate elevation
row): A time-bounded elevation is just a regular grant that happens to
carry an expiry timestamp. The sweeper reads rows where
``expires_at < now()`` and deletes them, reverting the agent to its
baseline permissions. A separate table would require a ``JOIN`` on
every resolve — unnecessary complexity for a two-column row scan. The
memory-expiry sweeper (G5.2) uses the same pattern: ``expires_at``
in-row, one periodic DELETE tick.

**Why this migration is ``0022`` with ``down_revision = "0019"``**
rather than folded into ``0019``: The grant-management REST surface
(this PR) and the base permission model (PR #1052 / migration ``0019``)
shipped in different PRs on the same initiative. Chaining ``0022``
after ``0019`` lets both PRs merge independently without requiring a
simultaneous rebase.

**Why the ADD COLUMN approach**: Migration ``0019`` already creates
the ``agent_permission`` table with all base columns. This migration
only extends that table — re-creating it would conflict at apply time
when both migrations are in the same Alembic history.

Dialect-portability decisions
-----------------------------

Mirrors the discipline 0001-0019 established:

* ``expires_at`` — nullable ``timestamptz`` (PG) / ``DateTime``
  (SQLite); no server default (NULL is the sensible initial value for
  every existing row, i.e. all grants are permanent until an operator
  sets an expiry).
* Index — b-tree via ``postgresql_using="btree"`` (no-op on SQLite).

Reversibility contract
----------------------

``downgrade()`` drops the index then the column, reversing
``upgrade()`` in order. The CI guard inspects only ``upgrade()``;
destructive ops in ``downgrade()`` are allowed by design.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0022"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_permission",
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "agent_permission_expires_at_idx",
        "agent_permission",
        ["expires_at"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    op.drop_index(
        "agent_permission_expires_at_idx",
        table_name="agent_permission",
    )
    op.drop_column("agent_permission", "expires_at")
