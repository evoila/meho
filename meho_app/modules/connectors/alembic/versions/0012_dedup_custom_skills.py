"""Clear duplicate custom_skill fields.

D-04: Data migration to clear custom_skill where it exactly matches
generated_skill (after TRIM). These duplicates waste ~28K chars per
K8s investigation by doubling the skill content in the system prompt.

Revision ID: connectors_0012_dedup_custom_skills
Revises: squash_001
Create Date: 2026-03-26
"""

import sqlalchemy as sa
from alembic import op

revision = "connectors_0012_dedup_custom_skills"
down_revision = "squash_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # D-04: Clear custom_skill where it exactly matches generated_skill
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
    # Cannot restore cleared custom_skills -- they were redundant duplicates
    pass
