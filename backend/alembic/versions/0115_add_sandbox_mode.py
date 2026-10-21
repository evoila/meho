# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add sandbox mode: the stored mode, the delete-grant table, the audit mark (#4622).

Revision ID: 0115
Revises: 0114
Create Date: 2026-10-21

Decision record: ``docs/decisions/sandbox-mode.md``. Three additive changes:

* ``sandbox_mode_state`` -- exactly one row (``id = 1``) that says whether
  the database belongs to an instance in sandbox mode. **A database that
  existed before this migration gets the row ``false`` here**, also when it
  holds no rows at all: an empty history does not prove a new install. A
  database created from empty by this release (the whole chain runs in one
  ``upgrade``) gets no row; MEHO stores its setting at the first start.
  So only a new database can hold ``true``. ``env.py`` records the
  revisions the upgrade started from in ``config.attributes`` so this
  script can tell the two cases apart; when that record is missing (for
  example offline mode) the row is stored as ``false``, the safe side.
* ``sandbox_delete_grant`` -- the delete grants, same shape and indexes as
  ``service_principal_grant``. An older build never reads this table, so
  after a rollback every delete waits for a person again.
* ``audit_log.sandbox_mode`` -- boolean, not null, database default
  ``false``. An older build does not know the column and still writes
  rows; every row from before this migration reads ``false``.

``downgrade()`` drops the two tables and the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

# revision identifiers, used by Alembic.
revision: str = "0115"
down_revision: str | None = "0114"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATE = "sandbox_mode_state"
_GRANTS = "sandbox_delete_grant"
_AUDIT = "audit_log"

#: The key ``env.py`` stores the starting revisions under.
STARTING_HEADS_ATTRIBUTE = "starting_heads"


def _database_existed_before_this_upgrade() -> bool:
    """Whether the upgrade started from a stamped revision (an existing database).

    ``env.py`` reads the current heads before any script runs and stores
    them in ``config.attributes``. An empty tuple means the database had
    no ``alembic_version`` row: it is being created from empty. A missing
    record means the environment did not say, and the safe answer is
    "existed" (store ``false``).
    """
    attributes = context.config.attributes
    if STARTING_HEADS_ATTRIBUTE not in attributes:
        return True
    return bool(attributes[STARTING_HEADS_ATTRIBUTE])


def upgrade() -> None:
    """Create the state table, the delete-grant table and the audit column."""
    op.create_table(
        _STATE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("sandbox_mode", sa.Boolean(), nullable=False),
        sa.Column("stored_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_sandbox_mode_state_single_row"),
    )
    if _database_existed_before_this_upgrade():
        op.execute(
            sa.text(
                "INSERT INTO sandbox_mode_state (id, sandbox_mode, stored_at) "
                "VALUES (1, false, CURRENT_TIMESTAMP)"
            )
        )

    op.create_table(
        _GRANTS,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("principal_sub", sa.Text(), nullable=False),
        sa.Column("op_id", sa.Text(), nullable=False),
        sa.Column("connector_id", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=True),
        sa.Column("target_product", sa.Text(), nullable=True),
        sa.Column("target_name_pattern", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_by_sub", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_sub", sa.Text(), nullable=True),
        sa.Column("revoked_reason", sa.Text(), nullable=True),
    )
    op.create_index(
        f"{_GRANTS}_lookup_idx",
        _GRANTS,
        ["tenant_id", "principal_sub", "op_id"],
    )
    op.create_index(
        f"uq_{_GRANTS}_targeted",
        _GRANTS,
        ["tenant_id", "principal_sub", "op_id", "connector_id", "target_id"],
        unique=True,
        postgresql_where=sa.text("target_id IS NOT NULL AND revoked_at IS NULL"),
        sqlite_where=sa.text("target_id IS NOT NULL AND revoked_at IS NULL"),
    )
    targetless_where = (
        "target_id IS NULL AND target_product IS NULL "
        "AND target_name_pattern IS NULL AND revoked_at IS NULL"
    )
    op.create_index(
        f"uq_{_GRANTS}_targetless",
        _GRANTS,
        ["tenant_id", "principal_sub", "op_id", "connector_id"],
        unique=True,
        postgresql_where=sa.text(targetless_where),
        sqlite_where=sa.text(targetless_where),
    )
    op.create_index(f"{_GRANTS}_expires_at_idx", _GRANTS, ["expires_at"])

    op.add_column(
        _AUDIT,
        sa.Column("sandbox_mode", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    """Drop the audit column and both tables."""
    op.drop_column(_AUDIT, "sandbox_mode")
    op.drop_index(f"{_GRANTS}_expires_at_idx", table_name=_GRANTS)
    op.drop_index(f"uq_{_GRANTS}_targetless", table_name=_GRANTS)
    op.drop_index(f"uq_{_GRANTS}_targeted", table_name=_GRANTS)
    op.drop_index(f"{_GRANTS}_lookup_idx", table_name=_GRANTS)
    op.drop_table(_GRANTS)
    op.drop_table(_STATE)
