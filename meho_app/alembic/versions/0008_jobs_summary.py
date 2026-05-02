# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Add document_summary to ingestion_jobs.

Revision ID: 0008_jobs_summary
Revises: 0007_jobs_resume
Create Date: 2026-04-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_jobs_summary"
down_revision = "0007_jobs_resume"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ingestion_jobs",
        sa.Column("document_summary", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ingestion_jobs", "document_summary")
