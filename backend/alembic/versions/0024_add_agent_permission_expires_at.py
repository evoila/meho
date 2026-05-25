# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``expires_at`` to ``agent_permission`` for G11.2-T6 grant elevation.

Revision ID: 0024
Revises: 0023
Create Date: 2026-05-25

This migration is the schema substrate of Task #819 (G11.2-T6) under
Initiative #803 (the P3 agent identity + RBAC + approval gate). It
extends the ``agent_permission`` table — created by migration
``0022_create_agent_permission`` (G11.2-T3, #820) — with a nullable
``expires_at`` timestamp column and a companion index that drives the
elevation-expiry sweeper tick.

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

**Why this migration is ``0024`` with ``down_revision = "0023"``**
rather than folded into ``0022``: The grant-management surface (this
PR, #1066) and the base permission model (PR #1052 / migration
``0022_create_agent_permission``) shipped in different PRs on the same
initiative. Chaining ``0024`` after the current head (``0023``,
T4's approval-request table) lets both PRs merge independently.

**Why the ADD COLUMN approach**: Migration ``0022`` (G11.2-T3 #820)
already creates the ``agent_permission`` table with all base columns.
This migration only extends that table — re-creating it would conflict
at apply time. (T6 originally shipped its own copy of the table; the
rebase onto #1052 deleted that duplicate so this is now a pure ALTER.)

Dialect-portability decisions
-----------------------------

Mirrors the discipline 0001-0023 established:

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
revision: str = "0024"
down_revision: str | None = "0023"
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
