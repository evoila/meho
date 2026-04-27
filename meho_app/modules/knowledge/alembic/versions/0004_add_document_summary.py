# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Add document_summary to ingestion_jobs.

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ingestion_jobs",
        sa.Column("document_summary", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ingestion_jobs", "document_summary")
