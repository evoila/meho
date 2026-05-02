# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Add scope_type and connector_type_scope to ingestion_jobs.

Enables filtering documents by scope so the global section shows only
global documents, not every job across all scopes.

Revision ID: 0006_jobs_scope
Revises: 0005_webhook_secret
Create Date: 2026-04-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_jobs_scope"
down_revision = "0005_webhook_secret"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ingestion_jobs",
        sa.Column("scope_type", sa.String(20), nullable=True),
    )
    op.add_column(
        "ingestion_jobs",
        sa.Column("connector_type_scope", sa.String(100), nullable=True),
    )
    op.create_index("idx_ingestion_jobs_scope_type", "ingestion_jobs", ["scope_type"])

    op.execute(
        sa.text(
            "UPDATE ingestion_jobs SET scope_type = 'instance' "
            "WHERE connector_id IS NOT NULL AND scope_type IS NULL"
        )
    )
    op.execute(
        sa.text(
            "UPDATE ingestion_jobs SET scope_type = 'global' "
            "WHERE connector_id IS NULL AND scope_type IS NULL"
        )
    )


def downgrade() -> None:
    op.drop_index("idx_ingestion_jobs_scope_type", table_name="ingestion_jobs")
    op.drop_column("ingestion_jobs", "connector_type_scope")
    op.drop_column("ingestion_jobs", "scope_type")
