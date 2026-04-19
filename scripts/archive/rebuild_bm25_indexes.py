#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Rebuild BM25 Indexes for All Tenants

Fixes the missing BM25 indexes issue by rebuilding them from existing knowledge chunks.

Usage:
    docker exec meho-meho-knowledge-1 python3 -m scripts.rebuild_bm25_indexes
    
Or from host (with venv):
    python scripts/rebuild_bm25_indexes.py
"""
import asyncio
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from meho_knowledge.database import get_session_maker
from meho_knowledge.repository import KnowledgeRepository
from meho_knowledge.schemas import KnowledgeChunkFilter
from meho_knowledge.bm25_index import BM25IndexManager
from uuid import UUID
import hashlib


async def rebuild_all_indexes():
    """Rebuild BM25 indexes for all tenants"""
    session_maker = get_session_maker()
    
    # Create BM25 manager
    index_dir = Path("./data/bm25_indexes")
    bm25_manager = BM25IndexManager(index_dir)
    
    print(f"\n{'='*80}")
    print(f"🔧 REBUILDING BM25 INDEXES")
    print(f"{'='*80}\n")
    
    async with session_maker() as session:
        repository = KnowledgeRepository(session)
        
        # Get all unique tenant_ids
        from sqlalchemy import select, distinct
        from meho_knowledge.models import KnowledgeChunkModel
        
        result = await session.execute(
            select(distinct(KnowledgeChunkModel.tenant_id))
        )
        tenant_ids = [row[0] for row in result]
        
        print(f"Found {len(tenant_ids)} tenants with knowledge chunks:\n")
        
        for tenant_id in tenant_ids:
            print(f"{'='*80}")
            print(f"📋 Tenant: {tenant_id}")
            print(f"{'='*80}")
            
            # Fetch all chunks for this tenant (get ALL chunks, handle pagination if needed)
            all_chunks = []
            offset = 0
            batch_size = 1000
            
            while True:
                batch = await repository.list_chunks(
                    KnowledgeChunkFilter(tenant_id=tenant_id, limit=batch_size, offset=offset)
                )
                if not batch:
                    break
                all_chunks.extend(batch)
                offset += batch_size
                if len(batch) < batch_size:
                    break  # Last batch
            
            chunks = all_chunks
            
            print(f"  Chunks found: {len(chunks)}")
            
            if not chunks:
                print(f"  ⚠️  No chunks - skipping")
                continue
            
            # Convert to BM25 documents
            documents = []
            for chunk in chunks:
                documents.append({
                    "id": chunk.id,
                    "text": chunk.text,
                    "metadata": chunk.search_metadata or {}
                })
            
            # Convert tenant_id to UUID
            try:
                tenant_uuid = UUID(tenant_id)
            except (ValueError, TypeError):
                # tenant_id is a string like "demo-tenant"
                tenant_hash = hashlib.md5(tenant_id.encode()).hexdigest()
                tenant_uuid = UUID(tenant_hash)
                print(f"  Tenant UUID: {tenant_uuid} (from MD5 hash)")
            
            # Build index
            print(f"  Building BM25 index...")
            try:
                await bm25_manager.build_index(
                    tenant_id=tenant_uuid,
                    documents=documents
                )
                
                # Verify it was created
                stats = await bm25_manager.get_index_stats(tenant_uuid)
                if stats.get("exists"):
                    print(f"  ✅ Index built successfully!")
                    print(f"     - Documents: {stats['num_documents']}")
                    print(f"     - Avg doc length: {stats['avg_doc_length']:.1f} tokens")
                    print(f"     - File: {tenant_uuid}.bm25.pkl")
                else:
                    print(f"  ❌ Index build failed (no stats)")
                    
            except Exception as e:
                print(f"  ❌ Index build failed: {e}")
            
            print()
        
        print(f"{'='*80}")
        print(f"✅ BM25 INDEX REBUILD COMPLETE")
        print(f"{'='*80}\n")
        
        # List created files
        index_files = list(index_dir.glob("*.pkl"))
        print(f"Created {len(index_files)} index files:")
        for file in index_files:
            size_kb = file.stat().st_size / 1024
            print(f"  - {file.name} ({size_kb:.1f} KB)")


if __name__ == "__main__":
    asyncio.run(rebuild_all_indexes())

