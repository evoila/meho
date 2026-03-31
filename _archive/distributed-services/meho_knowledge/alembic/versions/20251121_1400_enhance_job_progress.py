"""Enhance ingestion job progress tracking

Adds detailed progress fields for better UX (Task 29 - Session 30):
- Stage-based progress (extracting, chunking, embedding, storing)
- Stage progress (0.0-1.0) and overall progress (0.0-1.0)
- Human-readable status messages
- ETA estimation
- Enhanced error tracking (which stage, which chunk)
- Job retention for auto-cleanup

Revision ID: enhance_job_progress
Revises: add_pgvector
Create Date: 2025-11-21 14:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'enhance_job_progress'
down_revision = 'add_pgvector'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add detailed progress fields to ingestion_jobs"""
    # Detailed progress tracking
    op.add_column('ingestion_jobs', sa.Column('current_stage', sa.String(50), nullable=True))
    op.add_column('ingestion_jobs', sa.Column('stage_progress', sa.Float(), server_default='0.0'))
    op.add_column('ingestion_jobs', sa.Column('overall_progress', sa.Float(), server_default='0.0'))
    op.add_column('ingestion_jobs', sa.Column('status_message', sa.Text(), nullable=True))
    
    # Timing and estimation
    op.add_column('ingestion_jobs', sa.Column('stage_started_at', sa.TIMESTAMP(), nullable=True))
    op.add_column('ingestion_jobs', sa.Column('estimated_completion', sa.TIMESTAMP(), nullable=True))
    
    # Enhanced error tracking
    op.add_column('ingestion_jobs', sa.Column('error_stage', sa.String(50), nullable=True))
    op.add_column('ingestion_jobs', sa.Column('error_chunk_index', sa.Integer(), nullable=True))
    op.add_column('ingestion_jobs', sa.Column('error_details', postgresql.JSONB(), nullable=True))
    
    # Job retention (auto-cleanup)
    op.add_column('ingestion_jobs', sa.Column('retention_until', sa.TIMESTAMP(), nullable=True))
    
    # Index for cleanup task
    op.create_index('idx_ingestion_jobs_retention', 'ingestion_jobs', ['retention_until'])


def downgrade() -> None:
    """Remove detailed progress fields"""
    op.drop_index('idx_ingestion_jobs_retention', 'ingestion_jobs')
    op.drop_column('ingestion_jobs', 'retention_until')
    op.drop_column('ingestion_jobs', 'error_details')
    op.drop_column('ingestion_jobs', 'error_chunk_index')
    op.drop_column('ingestion_jobs', 'error_stage')
    op.drop_column('ingestion_jobs', 'estimated_completion')
    op.drop_column('ingestion_jobs', 'stage_started_at')
    op.drop_column('ingestion_jobs', 'status_message')
    op.drop_column('ingestion_jobs', 'overall_progress')
    op.drop_column('ingestion_jobs', 'stage_progress')
    op.drop_column('ingestion_jobs', 'current_stage')

