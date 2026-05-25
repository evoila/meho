# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the ``agent_permission`` table for G11.2-T6 grant management.

Revision ID: 0022
Revises: 0017
Create Date: 2026-05-25

This migration is the schema substrate of Task #819 (G11.2-T6) under
Initiative #803 (the P3 agent identity + RBAC + approval gate). It
adds the ``agent_permission`` table — one row per per-(principal,
op-pattern, target-scope) permission grant — and supports time-bounded
elevation via an ``expires_at`` timestamp column.

What this migration adds
------------------------

* The ``agent_permission`` table — per-tenant permission grants keyed
  on ``(tenant_id, principal_sub, op_pattern, target_scope)``.
* Index: ``agent_permission_tenant_principal_idx`` — b-tree on
  ``(tenant_id, principal_sub)`` driving the dominant resolver query.
* Index: ``agent_permission_expires_at_idx`` — b-tree on
  ``(expires_at)`` driving the elevation-expiry sweeper tick
  (``WHERE expires_at IS NOT NULL AND expires_at < now()``).
* CHECK constraint: ``ck_agent_permission_verdict`` — enforces the
  closed ``verdict IN ('auto-execute', 'needs-approval', 'deny')``
  vocabulary at the DB layer.

Design decisions
----------------

**Why ``expires_at`` is in this table** (not a separate elevation row):
A time-bounded elevation is just a regular grant that happens to carry
an expiry timestamp. The sweeper reads rows where ``expires_at < now()``
and deletes them, reverting the agent to its baseline permissions. A
separate table would require a ``JOIN`` on every resolve — unnecessary
complexity for a two-column row scan. The memory-expiry sweeper (G5.2)
uses the same pattern: ``expires_at`` in-row, one periodic DELETE tick.

**Why ``created_by_sub`` is NOT NULL**: Every grant is issued by an
authenticated ``tenant_admin``; the audit row carries the same subject.
Storing it inline avoids a JOIN to the audit log on every grants-list
call and survives log compaction.

**Why ``principal_sub`` has no FK**: The agent-principal / Keycloak
table is G11.2-T1 (#815)'s scope. Soft-FK discipline (opaque JWT
``sub``) mirrors :attr:`~meho_backplane.db.models.AgentDefinition.identity_ref`.

Dialect-portability decisions
-----------------------------

Mirrors the discipline 0001–0017 established:

* ``id`` server default — PG gets ``gen_random_uuid()``; SQLite leaves
  the column to the ORM ``default=uuid.uuid4``.
* ``created_at`` server default — PG gets ``now()``; SQLite leaves it
  to ``default=lambda: datetime.now(UTC)``.
* ``expires_at`` — nullable ``timestamptz`` (PG) / ``DateTime`` (SQLite);
  ORM-side ``default=None``.
* Index — b-tree via ``postgresql_using="btree"`` (no-op on SQLite).
* CHECK — ``sa.CheckConstraint`` with a named constraint; portable.

Reversibility contract
----------------------

``downgrade()`` drops indexes then the table, reversing ``upgrade()``
in order.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0022"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:  # noqa: PLR0912
    op.create_table(
        "agent_permission",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("principal_sub", sa.Text(), nullable=False),
        sa.Column("op_pattern", sa.Text(), nullable=False),
        sa.Column("target_scope", sa.Text(), nullable=True),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("created_by_sub", sa.Text(), nullable=False),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "verdict IN ('auto-execute', 'needs-approval', 'deny')",
            name="ck_agent_permission_verdict",
        ),
    )
    op.create_index(
        "agent_permission_tenant_principal_idx",
        "agent_permission",
        ["tenant_id", "principal_sub"],
        postgresql_using="btree",
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
    op.drop_index(
        "agent_permission_tenant_principal_idx",
        table_name="agent_permission",
    )
    op.drop_table("agent_permission")
