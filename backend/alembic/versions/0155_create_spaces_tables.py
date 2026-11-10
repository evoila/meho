# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create ``space``, ``space_member`` and ``resource_assignment``.

Revision ID: 0155
Revises: 0154
Create Date: 2026-11-10

A space is an area inside one tenant that belongs to a person or a small
team. These three tables hold the spaces, their members and the systems
assigned to them. A change never edits a member or assignment row: it sets
``valid_to`` on the old row and adds a new one, so the history stays
readable. Nothing is deleted.

The tables are new and empty, so the migration changes no behaviour on its
own. ``downgrade()`` drops the indexes and the tables.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0155"
down_revision: str | None = "0154"
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
    """Create the three tables and their indexes."""
    op.create_table(
        "space",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("owner_issuer", sa.Text(), nullable=False),
        sa.Column("owner_sub", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.CheckConstraint("kind IN ('person', 'team')", name="ck_space_kind"),
        sa.CheckConstraint("state IN ('active', 'disabled')", name="ck_space_state"),
    )
    op.create_index("space_tenant_name_idx", "space", ["tenant_id", "name"], unique=True)

    op.create_table(
        "space_member",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("space_id", sa.Uuid(), sa.ForeignKey("space.id"), nullable=False),
        sa.Column("member_issuer", sa.Text(), nullable=False),
        sa.Column("member_sub", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="active"),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("changed_by", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.CheckConstraint("role IN ('owner', 'member')", name="ck_space_member_role"),
        sa.CheckConstraint("state IN ('active', 'disabled')", name="ck_space_member_state"),
    )
    op.create_index(
        "space_member_tenant_space_idx", "space_member", ["tenant_id", "space_id", "valid_from"]
    )
    op.create_index(
        "space_member_open_idx",
        "space_member",
        ["space_id", "member_issuer", "member_sub"],
        unique=True,
        postgresql_where=sa.text("valid_to IS NULL"),
        sqlite_where=sa.text("valid_to IS NULL"),
    )

    op.create_table(
        "resource_assignment",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("space_id", sa.Uuid(), sa.ForeignKey("space.id"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        # No foreign key: a deleted target keeps its assignment history.
        sa.Column("target_id", sa.Uuid(), nullable=True),
        sa.Column("object_id", sa.Text(), nullable=True),
        sa.Column("name_at_assignment", sa.Text(), nullable=False),
        sa.Column("access", sa.Text(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("changed_by", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('target', 'object', 'service', 'fact')", name="ck_resource_assignment_kind"
        ),
        sa.CheckConstraint(
            "access IN ('own', 'shared_summary')", name="ck_resource_assignment_access"
        ),
    )
    op.create_index(
        "resource_assignment_tenant_space_idx",
        "resource_assignment",
        ["tenant_id", "space_id", "valid_from"],
    )
    op.create_index(
        "resource_assignment_tenant_target_idx",
        "resource_assignment",
        ["tenant_id", "target_id"],
    )


def downgrade() -> None:
    """Drop the indexes, then the tables."""
    op.drop_index("resource_assignment_tenant_target_idx", table_name="resource_assignment")
    op.drop_index("resource_assignment_tenant_space_idx", table_name="resource_assignment")
    op.drop_table("resource_assignment")
    op.drop_index("space_member_open_idx", table_name="space_member")
    op.drop_index("space_member_tenant_space_idx", table_name="space_member")
    op.drop_table("space_member")
    op.drop_index("space_tenant_name_idx", table_name="space")
    op.drop_table("space")
