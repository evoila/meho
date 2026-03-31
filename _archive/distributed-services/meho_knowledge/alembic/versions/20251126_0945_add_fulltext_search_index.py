"""Add GIN index for PostgreSQL full-text search

Replace BM25 pickle files with PostgreSQL built-in FTS.
Eliminates external file storage and shared volumes.

Revision ID: add_fulltext_search_index
Revises: enhance_job_progress
Create Date: 2025-11-26 09:45:00.000000
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'add_fulltext_search_index'
down_revision = 'enhance_job_progress'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Add GIN index for PostgreSQL full-text search.
    
    This enables fast keyword-based search using PostgreSQL's built-in FTS,
    replacing the external BM25 pickle file approach.
    
    Benefits:
    - Everything in PostgreSQL (no external files)
    - Automatic index maintenance
    - ACID guarantees
    - No shared volumes needed
    - Backup/replication handled by PostgreSQL
    """
    # Create GIN index for full-text search on text column
    # Using 'english' configuration for proper stemming and stop words
    op.execute('''
        CREATE INDEX IF NOT EXISTS idx_knowledge_chunk_text_fts
        ON knowledge_chunk
        USING GIN(to_tsvector('english', text))
    ''')
    
    # Note: Skipping tags index for now (tags are JSONB, complex to convert to tsvector)
    # Text-only FTS is sufficient for most use cases


def downgrade() -> None:
    """Remove full-text search indexes"""
    op.execute('DROP INDEX IF EXISTS idx_knowledge_chunk_text_fts')

