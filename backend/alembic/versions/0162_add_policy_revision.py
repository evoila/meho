# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keep the imported policy files.

Revision ID: 0162
Revises: 0161
Create Date: 2026-11-11

A new table ``policy_revision``: one row per imported policy file
(``meho policy import``), per tenant or for the whole install
(``tenant_id`` NULL). It holds who imported the file, who approved it,
when, the file as sent, the stored policy after the import and the diff.
MEHO keeps the last 20 per tenant.

The migration only adds a table. An older image never reads it, so a
rollback is safe. ``downgrade()`` drops it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0162"
down_revision: str | None = "0161"
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

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    """Add the ``policy_revision`` table."""
    op.create_table(
        "policy_revision",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("imported_by_sub", sa.Text(), nullable=False),
        sa.Column("approved_by_sub", sa.Text(), nullable=False),
        sa.Column("file_sha256", sa.Text(), nullable=False),
        sa.Column("file_text", sa.Text(), nullable=False),
        sa.Column("document", _JSON, nullable=False),
        sa.Column("diff", _JSON, nullable=False),
    )
    op.create_index("policy_revision_tenant_ts_idx", "policy_revision", ["tenant_id", "ts"])


def downgrade() -> None:
    """Drop the ``policy_revision`` table."""
    op.drop_index("policy_revision_tenant_ts_idx", table_name="policy_revision")
    op.drop_table("policy_revision")
