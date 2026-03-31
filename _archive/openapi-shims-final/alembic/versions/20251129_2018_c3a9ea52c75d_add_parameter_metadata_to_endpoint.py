"""add_parameter_metadata_to_endpoint

Revision ID: c3a9ea52c75d
Revises: b2c3d4e5f6g7
Create Date: 2025-11-29 20:18:04.725754

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c3a9ea52c75d'
down_revision = 'b2c3d4e5f6g7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add parameter_metadata column to endpoint_descriptor table
    # Provides explicit required/optional parameter structure for LLM guidance
    op.add_column(
        'endpoint_descriptor',
        sa.Column('parameter_metadata', sa.dialects.postgresql.JSONB(), nullable=True)
    )


def downgrade() -> None:
    # Remove parameter_metadata column
    op.drop_column('endpoint_descriptor', 'parameter_metadata')

