# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Add file_hash and storage_key to ingestion_jobs for checkpoint resume.

Revision ID: 0007_jobs_resume
Revises: 0006_jobs_scope
Create Date: 2026-04-15
"""

import sqlalchemy as sa
from alembic import op

revision = "0007_jobs_resume"
down_revision = "0006_jobs_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ingestion_jobs",
        sa.Column("file_hash", sa.String(64), nullable=True),
    )
    op.add_column(
        "ingestion_jobs",
        sa.Column("storage_key", sa.String(1024), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ingestion_jobs", "storage_key")
    op.drop_column("ingestion_jobs", "file_hash")
