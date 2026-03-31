"""add_tags_to_ingestion_jobs

Adds tags column to ingestion_jobs so document-level metadata can be surfaced
in APIs/UX without scanning every chunk.

Revision ID: 7f21c0b2a1f5
Revises: f61d7ebb5918
Create Date: 2025-11-19 16:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = '7f21c0b2a1f5'
down_revision = 'f61d7ebb5918'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add tags column with default empty list."""
    op.add_column(
        'ingestion_jobs',
        sa.Column(
            'tags',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default='[]'
        )
    )


def downgrade() -> None:
    """Remove tags column."""
    op.drop_column('ingestion_jobs', 'tags')

