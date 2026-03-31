"""add_ingestion_jobs_table

Adds ingestion_jobs table for tracking document/text ingestion progress.

Enables:
- User visibility (progress bars in frontend)
- Error reporting
- Reliable testing (poll for completion instead of sleep)
- Monitoring/observability

Revision ID: f61d7ebb5918
Revises: 939f465dfd93
Create Date: 2025-11-16 15:15:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'f61d7ebb5918'
down_revision = '939f465dfd93'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Create ingestion_jobs table.
    
    Tracks ingestion job progress and status for:
    - Document uploads
    - Text ingestion
    - Webhook events
    """
    op.create_table(
        'ingestion_jobs',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('job_type', sa.String(50), nullable=False),
        sa.Column('status', sa.String(50), nullable=False),
        sa.Column('tenant_id', sa.String(255), nullable=False),
        sa.Column('filename', sa.String(512), nullable=True),
        sa.Column('file_size', sa.Integer(), nullable=True),
        sa.Column('knowledge_type', sa.String(50), nullable=False),
        sa.Column('total_chunks', sa.Integer(), nullable=True),
        sa.Column('chunks_processed', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('chunks_created', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('chunk_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('started_at', sa.TIMESTAMP(), nullable=False),
        sa.Column('completed_at', sa.TIMESTAMP(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    
    # Create indexes for efficient queries
    op.create_index('idx_ingestion_jobs_status', 'ingestion_jobs', ['status'], unique=False)
    op.create_index('idx_ingestion_jobs_tenant', 'ingestion_jobs', ['tenant_id'], unique=False)
    op.create_index('idx_ingestion_jobs_tenant_status', 'ingestion_jobs', ['tenant_id', 'status'], unique=False)


def downgrade() -> None:
    """Drop ingestion_jobs table and indexes"""
    op.drop_index('idx_ingestion_jobs_tenant_status', table_name='ingestion_jobs')
    op.drop_index('idx_ingestion_jobs_tenant', table_name='ingestion_jobs')
    op.drop_index('idx_ingestion_jobs_status', table_name='ingestion_jobs')
    op.drop_table('ingestion_jobs')
