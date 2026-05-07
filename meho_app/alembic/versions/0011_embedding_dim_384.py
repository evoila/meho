# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Resize embedding columns from 1024 to 384 dimensions.

The knowledge stack switches from the TEI bge-m3 sidecar (1024-dim) to
in-process fastembed running ``sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2``
(384-dim). Three tables carry vector embeddings:

* ``knowledge_chunk.embedding``
* ``topology_embeddings.embedding``
* ``connector_memory.embedding``

pgvector's column dim is part of the column type, so we drop and recreate
each column at the new dim. Existing embeddings are destroyed; chunks /
entities / memories must be re-ingested. This is acceptable because the
fastembed path is positioned as a preview ahead of the MEHO.Knowledge
remote service taking over.

Revision ID: 0011_embedding_dim_384
Revises: 0010_license_issuance_audit
Create Date: 2026-05-05
"""

from alembic import op

revision = "0011_embedding_dim_384"
down_revision = "0010_license_issuance_audit"
branch_labels = None
depends_on = None


def _resize_embedding_column(
    table: str,
    *,
    index_name: str,
    new_dim: int,
    nullable: bool = True,
) -> None:
    """Drop+recreate ``{table}.embedding`` at ``new_dim`` and rebuild its HNSW index."""
    op.execute(f"DROP INDEX IF EXISTS {index_name}")
    op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS embedding")
    op.execute(
        f"ALTER TABLE {table} ADD COLUMN embedding vector({new_dim})"
        + ("" if nullable else " NOT NULL")
    )
    op.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {index_name}
        ON {table}
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        """
    )


def upgrade() -> None:
    _resize_embedding_column(
        "knowledge_chunk",
        index_name="idx_knowledge_chunk_embedding_hnsw",
        new_dim=384,
    )
    _resize_embedding_column(
        "topology_embeddings",
        index_name="idx_topology_embeddings_hnsw",
        new_dim=384,
    )
    _resize_embedding_column(
        "connector_memory",
        index_name="idx_connector_memory_embedding_hnsw",
        new_dim=384,
    )


def downgrade() -> None:
    # Symmetric reverse: back to 1024 dim. Existing 384-dim embeddings are
    # destroyed; re-ingestion required after a downgrade as well.
    _resize_embedding_column(
        "knowledge_chunk",
        index_name="idx_knowledge_chunk_embedding_hnsw",
        new_dim=1024,
    )
    _resize_embedding_column(
        "topology_embeddings",
        index_name="idx_topology_embeddings_hnsw",
        new_dim=1024,
    )
    _resize_embedding_column(
        "connector_memory",
        index_name="idx_connector_memory_embedding_hnsw",
        new_dim=1024,
    )
