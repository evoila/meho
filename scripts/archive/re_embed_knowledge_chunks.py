#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Re-embed all knowledge chunks with text-embedding-3-large.

This script regenerates embeddings for all existing knowledge chunks after
upgrading from text-embedding-3-small (1536D) to text-embedding-3-large (3072D).

Usage:
    python scripts/re_embed_knowledge_chunks.py [--batch-size 100] [--dry-run]

Prerequisites:
    1. Run database migration: alembic upgrade head
    2. Ensure OPENAI_API_KEY is set in environment
    3. Verify embedding_model config is set to "text-embedding-3-large"

Performance:
    - Batch size 100: ~100 chunks/minute (rate limits apply)
    - 10,000 chunks: ~100 minutes (~1.5-2 hours)
    - Cost: ~$0.39 for 10K chunks at 300 tokens each
"""
import asyncio
import sys
from pathlib import Path
from typing import List
from datetime import datetime
import argparse

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select, func
from meho_knowledge.database import get_session_maker
from meho_knowledge.models import KnowledgeChunkModel
from meho_knowledge.embeddings import OpenAIEmbeddings
from meho_core.config import get_config
import structlog

logger = structlog.get_logger(__name__)


async def count_chunks_needing_reembedding(session) -> int:
    """Count chunks with NULL embeddings (need re-embedding)."""
    stmt = select(func.count()).select_from(KnowledgeChunkModel).where(
        KnowledgeChunkModel.embedding.is_(None)
    )
    result = await session.execute(stmt)
    return result.scalar() or 0


async def count_total_chunks(session) -> int:
    """Count all chunks."""
    stmt = select(func.count()).select_from(KnowledgeChunkModel)
    result = await session.execute(stmt)
    return result.scalar() or 0


async def fetch_chunks_batch(
    session,
    offset: int,
    limit: int
) -> List[KnowledgeChunkModel]:
    """Fetch a batch of chunks that need re-embedding."""
    stmt = (
        select(KnowledgeChunkModel)
        .where(KnowledgeChunkModel.embedding.is_(None))
        .order_by(KnowledgeChunkModel.created_at)
        .offset(offset)
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def re_embed_all_chunks(
    batch_size: int = 100,
    dry_run: bool = False
):
    """
    Re-generate embeddings for all knowledge chunks.
    
    Args:
        batch_size: Number of chunks to process per batch (default: 100)
        dry_run: If True, only count chunks without making changes
    """
    config = get_config()
    
    # Verify configuration
    print(f"📋 Configuration:")
    print(f"  Embedding Model: {config.embedding_model}")
    print(f"  OpenAI API Key: {'✅ Set' if config.openai_api_key else '❌ Missing'}")
    print()
    
    if config.embedding_model != "text-embedding-3-large":
        print(f"⚠️  WARNING: Embedding model is '{config.embedding_model}'")
        print(f"   Expected: 'text-embedding-3-large'")
        print(f"   Update meho_core/config.py or set EMBEDDING_MODEL env var")
        print()
    
    if not config.openai_api_key:
        print("❌ ERROR: OPENAI_API_KEY not set!")
        sys.exit(1)
    
    # Initialize embedding provider
    embeddings = OpenAIEmbeddings(
        api_key=config.openai_api_key,
        model=config.embedding_model
    )
    
    print(f"✅ Embedding provider initialized:")
    print(f"  Model: {embeddings.model}")
    print(f"  Dimensions: {embeddings.dimension}")
    print()
    
    # Get database session
    session_maker = get_session_maker()
    
    async with session_maker() as session:
        # Count chunks
        total_chunks = await count_total_chunks(session)
        chunks_needing_embedding = await count_chunks_needing_reembedding(session)
        chunks_with_embedding = total_chunks - chunks_needing_embedding
        
        print(f"📊 Knowledge Chunk Statistics:")
        print(f"  Total chunks: {total_chunks}")
        print(f"  With embeddings: {chunks_with_embedding}")
        print(f"  Need re-embedding: {chunks_needing_embedding}")
        print()
        
        if chunks_needing_embedding == 0:
            print("✅ All chunks already have embeddings!")
            return
        
        if dry_run:
            print("🏃 Dry run mode - no changes will be made")
            return
        
        # Estimate cost and time
        avg_tokens_per_chunk = 300  # Conservative estimate
        total_tokens = chunks_needing_embedding * avg_tokens_per_chunk
        cost_per_million = 0.13  # text-embedding-3-large pricing
        estimated_cost = (total_tokens / 1_000_000) * cost_per_million
        estimated_minutes = chunks_needing_embedding / 100  # ~100 chunks/minute
        
        print(f"📈 Estimates:")
        print(f"  Tokens: ~{total_tokens:,} ({avg_tokens_per_chunk} avg per chunk)")
        print(f"  Cost: ~${estimated_cost:.2f}")
        print(f"  Time: ~{estimated_minutes:.0f} minutes")
        print()
        
        response = input("Continue with re-embedding? (yes/no): ")
        if response.lower() not in ['yes', 'y']:
            print("❌ Cancelled")
            return
        
        print()
        print("🚀 Starting re-embedding process...")
        print("=" * 60)
        
        start_time = datetime.now()
        processed = 0
        errors = 0
        offset = 0
        
        while processed < chunks_needing_embedding:
            # Fetch batch
            batch = await fetch_chunks_batch(session, offset, batch_size)
            
            if not batch:
                break
            
            # Extract texts
            texts = [chunk.text for chunk in batch]
            
            try:
                # Generate embeddings (batch API call)
                new_embeddings = await embeddings.embed_batch(texts)
                
                # Update chunks
                for chunk, embedding in zip(batch, new_embeddings):
                    chunk.embedding = embedding
                
                # Commit batch
                await session.commit()
                
                processed += len(batch)
                elapsed = (datetime.now() - start_time).total_seconds()
                rate = processed / elapsed if elapsed > 0 else 0
                remaining = chunks_needing_embedding - processed
                eta_seconds = remaining / rate if rate > 0 else 0
                
                print(f"✅ Processed {processed:,}/{chunks_needing_embedding:,} chunks "
                      f"({processed/chunks_needing_embedding*100:.1f}%) "
                      f"| Rate: {rate:.1f} chunks/sec "
                      f"| ETA: {eta_seconds/60:.0f}m")
                
            except Exception as e:
                logger.error(f"❌ Batch failed: {e}")
                errors += len(batch)
                await session.rollback()
                
                # Continue with next batch after error
                print(f"⚠️  Skipping {len(batch)} chunks due to error")
            
            offset += batch_size
        
        elapsed_total = (datetime.now() - start_time).total_seconds()
        
        print("=" * 60)
        print(f"✅ Re-embedding complete!")
        print()
        print(f"📊 Results:")
        print(f"  Processed: {processed:,} chunks")
        print(f"  Errors: {errors}")
        print(f"  Time: {elapsed_total/60:.1f} minutes")
        print(f"  Rate: {processed/elapsed_total:.1f} chunks/sec")
        print()
        
        # Verify
        async with session_maker() as verify_session:
            remaining = await count_chunks_needing_reembedding(verify_session)
            if remaining == 0:
                print("✅ Verification: All chunks now have embeddings!")
            else:
                print(f"⚠️  Verification: {remaining} chunks still need re-embedding")
                print("   (Run script again to process remaining chunks)")


def main():
    parser = argparse.ArgumentParser(
        description="Re-embed all knowledge chunks with text-embedding-3-large"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Number of chunks to process per batch (default: 100)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count chunks without making changes"
    )
    
    args = parser.parse_args()
    
    asyncio.run(re_embed_all_chunks(
        batch_size=args.batch_size,
        dry_run=args.dry_run
    ))


if __name__ == "__main__":
    main()

