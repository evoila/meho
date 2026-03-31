"""Add llm_instructions column to endpoint_descriptor

Revision ID: 7a1b2c3d4e5f
Revises: c3a9ea52c75d
Create Date: 2025-11-30

This migration adds the llm_instructions JSONB column to the endpoint_descriptor
table as part of TASK-81 (LLM-Guided Schema Navigation).

The llm_instructions column stores per-endpoint guidance that teaches the LLM
how to help users through complex parameter collection for write operations.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers
revision = '7a1b2c3d4e5f'
down_revision = 'c3a9ea52c75d'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add llm_instructions column to endpoint_descriptor table."""
    op.add_column(
        'endpoint_descriptor',
        sa.Column('llm_instructions', JSONB, nullable=True)
    )


def downgrade() -> None:
    """Remove llm_instructions column from endpoint_descriptor table."""
    op.drop_column('endpoint_descriptor', 'llm_instructions')

