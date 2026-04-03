#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Build BM25 indexes for all existing tenants.

This script should be run once after deploying hybrid search to index existing knowledge.
For new documents, the ingestion service automatically builds/updates indexes.

Usage:
    python scripts/build_bm25_indexes.py
"""
import asyncio
import sys
import hashlib
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from meho_knowledge.bm25_index import BM25IndexManager
from meho_knowledge.database import get_session_maker
from meho_knowledge.models import KnowledgeChunkModel
from sqlalchemy import select
from uuid import UUID


async def build_indexes():
    """Build BM25 indexes for all tenants with knowledge chunks."""
    
    # Initialize BM25 manager
    index_dir = Path("./data/bm25_indexes")
    bm25_manager = BM25IndexManager(index_dir)
    
    # Get database session
    session_maker = get_session_maker()
    
    async with session_maker() as session:
        # Get all unique tenant IDs
        stmt = select(KnowledgeChunkModel.tenant_id).distinct()
        result = await session.execute(stmt)
        tenant_ids = [row[0] for row in result.all() if row[0] is not None]
        
        print(f"Found {len(tenant_ids)} tenants with knowledge chunks")
        
        for tenant_id in tenant_ids:
            print(f"\n🔨 Building BM25 index for tenant: {tenant_id}")
            
            # Get all chunks for this tenant
            stmt = select(KnowledgeChunkModel).where(
                KnowledgeChunkModel.tenant_id == tenant_id
            )
            result = await session.execute(stmt)
            chunks = result.scalars().all()
            
            print(f"   Found {len(chunks)} chunks")
            
            # Convert to BM25 documents
            documents = [
                {
                    "id": str(chunk.id),
                    "text": chunk.text,
                    "metadata": chunk.search_metadata or {}
                }
                for chunk in chunks
            ]
            
            # Build index
            try:
                # Convert string tenant_id to UUID using MD5 hash (same as hybrid search)
                try:
                    tenant_uuid = UUID(tenant_id)
                except (ValueError, AttributeError):
                    # tenant_id is a string like "demo-tenant", convert to UUID
                    tenant_hash = hashlib.md5(tenant_id.encode()).hexdigest()
                    tenant_uuid = UUID(tenant_hash)
                    print(f"   Converted '{tenant_id}' to UUID: {tenant_uuid}")
                
                await bm25_manager.build_index(
                    tenant_id=tenant_uuid,
                    documents=documents
                )
                print(f"   ✅ Index built successfully")
                
                # Get stats
                stats = await bm25_manager.get_index_stats(tenant_uuid)
                print(f"   📊 Stats: {stats['num_documents']} documents, avg length: {stats['avg_doc_length']:.1f} tokens")
                
            except Exception as e:
                print(f"   ❌ Failed to build index: {e}")
                continue
        
        print(f"\n✨ Complete! Built indexes for {len(tenant_ids)} tenants")


if __name__ == "__main__":
    asyncio.run(build_indexes())

