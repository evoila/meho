# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Remove delegate_credentials from webhook_registration.

Credential delegation is now always active for webhooks.
The credential resolver's automated chain (service -> delegated -> fail)
handles credential resolution without needing an explicit opt-in flag.

Revision ID: connectors_0013_remove_delegate_credentials
Revises: connectors_0012_dedup_custom_skills
Create Date: 2026-03-26
"""

import sqlalchemy as sa
from alembic import op

revision = "connectors_0013_remove_delegate_credentials"
down_revision = "connectors_0012_dedup_custom_skills"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("webhook_registration", "delegate_credentials")


def downgrade() -> None:
    op.add_column(
        "webhook_registration",
        sa.Column("delegate_credentials", sa.Boolean(), nullable=False, server_default="false"),
    )
