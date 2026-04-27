#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Compare BM25-only vs BM25+Hybrid search results.

TASK-126: Unified Connector Search Architecture

This script runs test queries against both search algorithms and shows
side-by-side comparison of results to help evaluate search quality.

Usage:
    python scripts/compare_search_algorithms.py

Prerequisites:
    1. Database is up and running
    2. Migration script has been run (knowledge_chunk entries exist)
    3. Environment variables set (DATABASE_URL, REDIS_URL, OPENAI_API_KEY)
"""
import asyncio
import sys
import logging
from pathlib import Path
from typing import List, Dict, Any
from uuid import UUID

# Suppress debug logging for cleaner output
logging.getLogger("meho_knowledge").setLevel(logging.WARNING)
logging.getLogger("structlog").setLevel(logging.WARNING)

# Configure structlog to not output anything below WARNING
import structlog
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
)

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from meho_core.config import get_config
from meho_knowledge.bm25_service import BM25Service
from meho_knowledge.bm25_hybrid_service import BM25HybridService
from meho_knowledge.embeddings import OpenAIEmbeddings
from meho_knowledge.models import KnowledgeChunkModel
from meho_openapi.models import ConnectorModel


# Test queries - natural language questions that should find VMware operations
TEST_QUERIES = [
    # Basic VM queries
    "give me a list of all vms in our vcenter",
    "list virtual machines",
    "show all VMs",
    
    # Health/status queries
    "check cluster health",
    "show host status",
    "get datastore performance",
    
    # Specific operations
    "power on a VM",
    "create a snapshot",
    "migrate virtual machine",
    
    # Natural language variations
    "what's running on the hosts",
    "how much memory is available",
    "find VMs with high CPU",
]


def format_result(result: Dict[str, Any], rank: int, algorithm: str) -> Dict[str, str]:
    """Format a single search result for display."""
    metadata = result.get("metadata", {})
    
    # Get score based on algorithm
    if algorithm == "hybrid":
        score = result.get("rrf_score", 0)
        bm25_rank = result.get("bm25_rank", "-")
        semantic_rank = result.get("semantic_rank", "-")
        score_detail = f"RRF={score:.4f} (BM25#{bm25_rank}, Sem#{semantic_rank})"
    else:
        score = result.get("bm25_score", 0)
        score_detail = f"BM25={score:.4f}"
    
    operation_id = metadata.get("operation_id", "N/A")
    operation_name = metadata.get("operation_name", "N/A")
    category = metadata.get("category", "N/A")
    
    return {
        "rank": rank,
        "operation_id": operation_id[:30] if operation_id else "N/A",
        "operation_name": operation_name[:40] if operation_name else "N/A",
        "category": category,
        "score": score_detail,
    }


async def run_comparison():
    """Run search comparison between BM25-only and hybrid."""
    config = get_config()
    
    print("=" * 80)
    print("TASK-126: Search Algorithm Comparison")
    print("=" * 80)
    print()
    
    # Create database connection
    engine = create_async_engine(config.database_url, echo=False)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    
    async with session_maker() as session:
        # Get tenant_id from a connector
        stmt = select(ConnectorModel).where(ConnectorModel.connector_type == "vmware").limit(1)
        result = await session.execute(stmt)
        connector = result.scalar_one_or_none()
        
        if not connector:
            print("❌ No VMware connector found. Please create one first.")
            return
        
        tenant_id = str(connector.tenant_id)
        connector_id = str(connector.id)
        
        print(f"📋 Configuration:")
        print(f"   Tenant ID: {tenant_id}")
        print(f"   Connector: {connector.name} ({connector_id})")
        print(f"   Embedding Model: {config.embedding_model}")
        print()
        
        # Count chunks
        count_stmt = select(KnowledgeChunkModel).where(
            KnowledgeChunkModel.tenant_id == tenant_id,
            KnowledgeChunkModel.search_metadata["source_type"].astext == "connector_operation"
        )
        count_result = await session.execute(count_stmt)
        chunks = count_result.scalars().all()
        print(f"📊 Knowledge chunks available: {len(chunks)}")
        print()
        
        # Initialize services
        embedding_provider = OpenAIEmbeddings(
            api_key=config.openai_api_key,
            model=config.embedding_model
        )
        
        # Note: Redis is optional but improves BM25 performance
        try:
            import redis.asyncio as redis
            redis_client = redis.from_url(config.redis_url)
            await redis_client.ping()
            print("✅ Redis connected (BM25 caching enabled)")
        except Exception as e:
            print(f"⚠️  Redis not available: {e}")
            redis_client = None
        
        bm25_service = BM25Service(session, redis_client)
        hybrid_service = BM25HybridService(session, embedding_provider, redis_client)
        
        # Metadata filters for connector operations
        metadata_filters = {
            "source_type": "connector_operation",
            "connector_type": "vmware",
        }
        
        print()
        print("=" * 80)
        print("Running test queries...")
        print("=" * 80)
        
        for query in TEST_QUERIES:
            print()
            print(f"🔍 Query: \"{query}\"")
            print("-" * 80)
            
            # Run BM25-only search
            bm25_results = await bm25_service.search(
                tenant_id=tenant_id,
                query=query,
                top_k=5,
                metadata_filters=metadata_filters
            )
            
            # Run hybrid search
            hybrid_results = await hybrid_service.search(
                tenant_id=tenant_id,
                query=query,
                top_k=5,
                metadata_filters=metadata_filters
            )
            
            # Format results
            print()
            print("BM25-Only Results:")
            if bm25_results:
                print(f"   {'#':<3} {'Operation ID':<32} {'Name':<35} {'Score'}")
                print(f"   {'-'*3} {'-'*32} {'-'*35} {'-'*20}")
                for i, r in enumerate(bm25_results[:5]):
                    fr = format_result(r, i+1, "bm25")
                    print(f"   {fr['rank']:<3} {fr['operation_id']:<32} {fr['operation_name']:<35} {fr['score']}")
            else:
                print("   (no results)")
            
            print()
            print("Hybrid (BM25 + Semantic) Results:")
            if hybrid_results:
                print(f"   {'#':<3} {'Operation ID':<32} {'Name':<35} {'Score'}")
                print(f"   {'-'*3} {'-'*32} {'-'*35} {'-'*20}")
                for i, r in enumerate(hybrid_results[:5]):
                    fr = format_result(r, i+1, "hybrid")
                    print(f"   {fr['rank']:<3} {fr['operation_id']:<32} {fr['operation_name']:<35} {fr['score']}")
            else:
                print("   (no results)")
            
            # Compare top results
            bm25_top = bm25_results[0].get("metadata", {}).get("operation_id") if bm25_results else None
            hybrid_top = hybrid_results[0].get("metadata", {}).get("operation_id") if hybrid_results else None
            
            if bm25_top and hybrid_top:
                if bm25_top == hybrid_top:
                    print(f"\n   ✅ Same top result: {bm25_top}")
                else:
                    print(f"\n   ⚠️  Different top results:")
                    print(f"      BM25:   {bm25_top}")
                    print(f"      Hybrid: {hybrid_top}")
            
            print()
        
        # Cleanup
        if redis_client:
            await redis_client.close()
        
        await engine.dispose()
    
    print("=" * 80)
    print("Comparison complete!")
    print()
    print("Key observations:")
    print("- BM25 is better for exact keyword matches (e.g., 'list VMs')")
    print("- Hybrid adds semantic matching (e.g., 'what's running' → list operations)")
    print("- Check results where algorithms differ for quality assessment")
    print("=" * 80)


def main():
    """Entry point."""
    asyncio.run(run_comparison())


if __name__ == "__main__":
    main()

