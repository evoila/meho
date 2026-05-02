# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Remove delegate_credentials from webhook_registration.

Credential delegation is now always active for webhooks. The credential
resolver's automated chain (service -> delegated -> fail) handles
resolution without an explicit opt-in flag.

Revision ID: 0003_rm_delegate_cred
Revises: 0002_dedup_skills
Create Date: 2026-03-26
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_rm_delegate_cred"
down_revision = "0002_dedup_skills"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("webhook_registration", "delegate_credentials")


def downgrade() -> None:
    op.add_column(
        "webhook_registration",
        sa.Column("delegate_credentials", sa.Boolean(), nullable=False, server_default="false"),
    )
