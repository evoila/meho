"""Add pgvector extension and embedding column

Migrates from Qdrant to PostgreSQL pgvector for vector storage.
Eliminates dual-database sync issues.

Revision ID: add_pgvector
Revises: add_search_metadata_column
Create Date: 2025-11-20 19:30:00.000000
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'add_pgvector'
down_revision = 'add_search_metadata_column'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Add pgvector extension and embedding column.
    
    This migration enables PostgreSQL to handle vector embeddings natively,
    eliminating the need for Qdrant and preventing sync drift issues.
    
    Note: Uses raw SQL via op.execute() because pgvector's vector type is not
    available in standard SQLAlchemy types. This is the recommended approach
    from the pgvector documentation for Alembic migrations.
    """
    # 1. Enable pgvector extension
    op.execute('CREATE EXTENSION IF NOT EXISTS vector')
    
    # 2. Add embedding column (1536 dimensions for OpenAI text-embedding-3-small)
    # Using raw SQL because pgvector vector type requires the extension
    op.execute('''
        ALTER TABLE knowledge_chunk 
        ADD COLUMN IF NOT EXISTS embedding vector(1536)
    ''')
    
    # 3. Create HNSW index for fast cosine similarity search
    # HNSW parameters:
    #   m = 16: max connections per layer (16 is good for most use cases)
    #   ef_construction = 64: candidates during index build (64 balances speed/quality)
    op.execute('''
        CREATE INDEX IF NOT EXISTS idx_knowledge_chunk_embedding_hnsw 
        ON knowledge_chunk 
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    ''')
    
    # 4. Create GIN indexes on metadata JSONB for fast filtering
    # These enable efficient metadata filtering during vector search
    op.execute('''
        CREATE INDEX IF NOT EXISTS idx_knowledge_chunk_metadata_resource_type
        ON knowledge_chunk ((search_metadata->>'resource_type'))
        WHERE search_metadata->>'resource_type' IS NOT NULL
    ''')
    
    op.execute('''
        CREATE INDEX IF NOT EXISTS idx_knowledge_chunk_metadata_content_type
        ON knowledge_chunk ((search_metadata->>'content_type'))
        WHERE search_metadata->>'content_type' IS NOT NULL
    ''')
    
    op.execute('''
        CREATE INDEX IF NOT EXISTS idx_knowledge_chunk_metadata_chapter
        ON knowledge_chunk ((search_metadata->>'chapter'))
        WHERE search_metadata->>'chapter' IS NOT NULL
    ''')
    
    # 5. Add index on knowledge_type for lifecycle queries
    op.execute('''
        CREATE INDEX IF NOT EXISTS idx_knowledge_chunk_knowledge_type
        ON knowledge_chunk (knowledge_type)
    ''')


def downgrade() -> None:
    """Remove pgvector support (if rolling back to Qdrant)"""
    op.execute('DROP INDEX IF EXISTS idx_knowledge_chunk_knowledge_type')
    op.execute('DROP INDEX IF EXISTS idx_knowledge_chunk_metadata_chapter')
    op.execute('DROP INDEX IF EXISTS idx_knowledge_chunk_metadata_content_type')
    op.execute('DROP INDEX IF EXISTS idx_knowledge_chunk_metadata_resource_type')
    op.execute('DROP INDEX IF EXISTS idx_knowledge_chunk_embedding_hnsw')
    op.execute('ALTER TABLE knowledge_chunk DROP COLUMN IF EXISTS embedding')
    op.execute('DROP EXTENSION IF EXISTS vector')

