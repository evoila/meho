# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add the run approval table and its tenant setting (#4126).

Revision ID: 0141
Revises: 0140
Create Date: 2026-11-07

A run approval lets a person approve the operation list of one automation
run once. Two additive changes:

* ``run_approval`` -- one row per request: the service principal (``sub``),
  the run reference (``work_ref``), the JSON list of operations, and the
  times it was requested, approved, revoked or ended. A call matches only
  an approved row that is still live, so a new table changes nothing until
  a person approves a request.
* ``tenant.run_approval_target_kinds_enabled`` -- the tenant setting that
  lets a run approval list a target kind (a product) instead of one
  target. ``false`` by default, so existing tenants keep exact targets.

An older build never reads either, so after a rollback every listed call
waits for approval again. ``downgrade()`` drops both.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0141"
down_revision: str | None = "0140"
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

_TABLE = "run_approval"


def upgrade() -> None:
    """Create the run approval table and add the tenant setting."""
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("principal_sub", sa.Text(), nullable=False),
        sa.Column("work_ref", sa.Text(), nullable=False),
        sa.Column(
            "entries",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by_sub", sa.Text(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_sub", sa.Text(), nullable=True),
        sa.Column("revoked_reason", sa.Text(), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "run_approval_lookup_idx",
        _TABLE,
        ["tenant_id", "principal_sub", "work_ref"],
    )
    with op.batch_alter_table("tenant") as batch_op:
        batch_op.add_column(
            sa.Column(
                "run_approval_target_kinds_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    """Drop the tenant setting and the run approval table."""
    with op.batch_alter_table("tenant") as batch_op:
        batch_op.drop_column("run_approval_target_kinds_enabled")
    op.drop_index("run_approval_lookup_idx", table_name=_TABLE)
    op.drop_table(_TABLE)
