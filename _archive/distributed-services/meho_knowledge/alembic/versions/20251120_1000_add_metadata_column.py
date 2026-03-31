"""Add search_metadata column for enhanced retrieval.

This migration adds a JSONB search_metadata column to store structured information
about each chunk for enhanced retrieval capabilities:
- Document structure (chapter, section)
- API-specific metadata (endpoints, HTTP methods, resource types)
- Content classification (content_type, has_json_example, etc.)
- Keywords for improved search

Note: Using 'search_metadata' instead of 'metadata' because 'metadata' is reserved in SQLAlchemy.

Revision ID: add_search_metadata_column
Revises: 7f21c0b2a1f5
Create Date: 2025-11-20 10:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = 'add_search_metadata_column'
down_revision = '7f21c0b2a1f5'
branch_labels = None
depends_on = None

TABLE_NAME = "knowledge_chunk"


def _table_exists(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _column_exists(inspector, table_name: str, column_name: str) -> bool:
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def _index_exists(inspector, table_name: str, index_name: str) -> bool:
    return any(idx["name"] == index_name for idx in inspector.get_indexes(table_name))


def upgrade() -> None:
    """
    Add search_metadata JSONB column for enhanced knowledge retrieval.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Check table exists
    if not _table_exists(inspector, TABLE_NAME):
        raise ValueError(f"Table {TABLE_NAME} does not exist. Run earlier migrations first.")

    # Add search_metadata column if it doesn't exist
    if not _column_exists(inspector, TABLE_NAME, 'search_metadata'):
        op.add_column(
            TABLE_NAME,
            sa.Column('search_metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=True, server_default='{}')
        )

    # Create GIN indexes on common metadata fields for efficient filtering
    # These enable fast filtering by chapter, content_type, endpoint_path, etc.
    
    if not _index_exists(inspector, TABLE_NAME, 'idx_chunks_search_metadata_endpoint'):
        op.execute(
            f"CREATE INDEX idx_chunks_search_metadata_endpoint ON {TABLE_NAME} USING gin ((search_metadata->'endpoint_path'))"
        )
    
    if not _index_exists(inspector, TABLE_NAME, 'idx_chunks_search_metadata_resource'):
        op.execute(
            f"CREATE INDEX idx_chunks_search_metadata_resource ON {TABLE_NAME} USING gin ((search_metadata->'resource_type'))"
        )
    
    if not _index_exists(inspector, TABLE_NAME, 'idx_chunks_search_metadata_content_type'):
        op.execute(
            f"CREATE INDEX idx_chunks_search_metadata_content_type ON {TABLE_NAME} USING gin ((search_metadata->'content_type'))"
        )
    
    if not _index_exists(inspector, TABLE_NAME, 'idx_chunks_search_metadata_chapter'):
        op.execute(
            f"CREATE INDEX idx_chunks_search_metadata_chapter ON {TABLE_NAME} USING gin ((search_metadata->'chapter'))"
        )


def downgrade() -> None:
    """
    Remove search_metadata column and indexes.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _table_exists(inspector, TABLE_NAME):
        return

    # Drop indexes
    for index_name in [
        'idx_chunks_search_metadata_chapter',
        'idx_chunks_search_metadata_content_type',
        'idx_chunks_search_metadata_resource',
        'idx_chunks_search_metadata_endpoint',
    ]:
        if _index_exists(inspector, TABLE_NAME, index_name):
            op.drop_index(index_name, table_name=TABLE_NAME)

    # Drop column
    if _column_exists(inspector, TABLE_NAME, 'search_metadata'):
        op.drop_column(TABLE_NAME, 'search_metadata')

