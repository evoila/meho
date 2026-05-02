# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Clear duplicate custom_skill fields.

D-04 data migration: clear ``custom_skill`` where it exactly matches
``generated_skill`` (after TRIM). Duplicates wasted ~28K chars per
Kubernetes investigation by doubling skill content in the system prompt.

Revision ID: 0002_dedup_skills
Revises: 0001_init
Create Date: 2026-03-26
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_dedup_skills"
down_revision = "0001_init"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text("""
            UPDATE connector
            SET custom_skill = NULL, updated_at = NOW()
            WHERE custom_skill IS NOT NULL
              AND generated_skill IS NOT NULL
              AND TRIM(custom_skill) = TRIM(generated_skill)
        """)
    )


def downgrade() -> None:
    raise NotImplementedError(
        "0002_dedup_skills is a one-way data migration. Cleared custom_skill "
        "rows that exactly matched generated_skill are not restorable from "
        "migration state alone -- restore from a database backup if you need "
        "the data back."
    )
