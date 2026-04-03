#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Re-embed all knowledge chunks and topology entities with Voyage AI.

Run after migrating from OpenAI to Voyage AI embeddings.
Alembic migration nulls out old embeddings -- this script regenerates them.

Usage (from host machine):
    ./scripts/re-embed.sh --dry-run          # Preview via Docker
    ./scripts/re-embed.sh                     # Full re-embed via Docker
    ./scripts/re-embed.sh --knowledge-only    # Only knowledge chunks
    ./scripts/re-embed.sh --batch-size 500    # Custom batch size

Usage (inside container):
    python scripts/re_embed_voyage.py --dry-run
    python scripts/re_embed_voyage.py --batch-size 500
    python scripts/re_embed_voyage.py --knowledge-only
    python scripts/re_embed_voyage.py --topology-only

Prerequisites:
    1. Run database migrations: ./scripts/migrate-all.sh
    2. Ensure VOYAGE_API_KEY is set in environment
    3. Ensure embedding_model config is set to "voyage-4-large"

Performance:
    - Voyage AI supports up to 1000 items per batch
    - Rate: ~200 embeddings/second with voyage-4-large
    - Cost: ~$0.05 per 1M tokens (voyage-4-large pricing)
"""
import asyncio
import os
import sys
from pathlib import Path
from datetime import datetime
import argparse

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select, func, text, update
from meho_app.database import get_session_maker
from meho_app.modules.knowledge.models import KnowledgeChunkModel
from meho_app.modules.topology.models import TopologyEmbeddingModel, TopologyEntityModel
from meho_app.modules.knowledge.embeddings import VoyageAIEmbeddings
from meho_app.core.config import get_config
import structlog

logger = structlog.get_logger(__name__)

MSG_RUN_MIGRATIONS = "ERROR: Run migrations first -- ./scripts/migrate-all.sh"


async def preflight_check() -> None:
    """
    Pre-flight validation: check DB, VOYAGE_API_KEY, and migration state.

    Exits with a clear error message if any prerequisite is missing.
    """
    config = get_config()

    print("\n  Pre-flight checks:")

    # 1. Check VOYAGE_API_KEY
    voyage_key = os.environ.get("VOYAGE_API_KEY") or config.voyage_api_key
    if not voyage_key:
        print("    FAIL: VOYAGE_API_KEY not set in environment or config")
        sys.exit("ERROR: VOYAGE_API_KEY not set. Add it to .env or export it.")
    print("    VOYAGE_API_KEY: Set")

    # 2. Check database connectivity
    try:
        session_maker = get_session_maker()
        async with session_maker() as session:
            await session.execute(text("SELECT 1"))
        print("    DB: Connected")
    except Exception as e:
        print(f"    FAIL: Cannot connect to database: {e}")
        sys.exit("ERROR: Database not reachable. Is PostgreSQL running?")

    # 3. Check migration state — embedding column should be 1024D (not 1536D)
    try:
        session_maker = get_session_maker()
        async with session_maker() as session:
            # Check if knowledge_chunk table exists and get embedding dimension
            result = await session.execute(text(
                "SELECT atttypmod FROM pg_attribute "
                "WHERE attrelid = 'knowledge_chunk'::regclass "
                "AND attname = 'embedding'"
            ))
            row = result.fetchone()
            if row is None:
                print("    FAIL: knowledge_chunk.embedding column not found")
                sys.exit(MSG_RUN_MIGRATIONS)
            dimension = row[0]
            if dimension == 1536:
                print("    FAIL: Embedding dimension is 1536 (old OpenAI). Expected 1024 (Voyage AI).")
                sys.exit(MSG_RUN_MIGRATIONS)
            elif dimension == 1024:
                print("    Migrations: Applied (1024D)")
            else:
                # Could be -1 (unset) or another value — just report it
                print(f"    Migrations: Embedding dimension is {dimension} (expected 1024)")
    except Exception as e:
        if "does not exist" in str(e):
            print("    FAIL: knowledge_chunk table does not exist")
            sys.exit(MSG_RUN_MIGRATIONS)
        else:
            print(f"    WARNING: Could not verify migration state: {e}")
            print("    Continuing anyway — verify manually that migrations are applied.")

    print("    Pre-flight OK: DB connected, VOYAGE_API_KEY set, migrations applied (1024D)\n")


async def count_null_embeddings(session, model_class, column) -> int:
    """Count rows with NULL embeddings."""
    stmt = select(func.count()).select_from(model_class).where(column.is_(None))
    result = await session.execute(stmt)
    return result.scalar() or 0


async def count_total(session, model_class) -> int:
    """Count all rows."""
    stmt = select(func.count()).select_from(model_class)
    result = await session.execute(stmt)
    return result.scalar() or 0


async def re_embed_knowledge_chunks(
    session,
    embeddings: VoyageAIEmbeddings,
    batch_size: int,
    dry_run: bool,
) -> tuple[int, int]:
    """Re-embed knowledge chunks with NULL embeddings.

    Returns:
        Tuple of (processed, errors)
    """
    total = await count_total(session, KnowledgeChunkModel)
    null_count = await count_null_embeddings(
        session, KnowledgeChunkModel, KnowledgeChunkModel.embedding
    )
    has_embedding = total - null_count

    print("\n  Knowledge Chunks:")
    print(f"    Total: {total}")
    print(f"    With embeddings: {has_embedding}")
    print(f"    Need re-embedding: {null_count}")

    if null_count == 0:
        print("    All chunks already have embeddings.")
        return 0, 0

    if dry_run:
        print("    [DRY RUN] No changes will be made.")
        return 0, 0

    processed = 0
    errors = 0
    offset = 0

    while True:
        stmt = (
            select(KnowledgeChunkModel)
            .where(KnowledgeChunkModel.embedding.is_(None))
            .order_by(KnowledgeChunkModel.created_at)
            .offset(offset)
            .limit(batch_size)
        )
        result = await session.execute(stmt)
        batch = list(result.scalars().all())

        if not batch:
            break

        texts = [chunk.text for chunk in batch]

        try:
            new_embeddings = await embeddings.embed_batch(texts, input_type="document")

            for chunk, embedding in zip(batch, new_embeddings):
                chunk.embedding = embedding

            await session.commit()
            processed += len(batch)
            print(f"    Processed {processed}/{null_count} chunks "
                  f"({processed / null_count * 100:.1f}%)")

        except Exception as e:
            logger.error("Batch failed", error=str(e))
            errors += len(batch)
            await session.rollback()
            print(f"    ERROR: Skipping {len(batch)} chunks: {e}")

        offset += batch_size

    return processed, errors


async def re_embed_topology_entities(
    session,
    embeddings: VoyageAIEmbeddings,
    batch_size: int,
    dry_run: bool,
) -> tuple[int, int]:
    """Re-embed topology entities with NULL embeddings.

    Returns:
        Tuple of (processed, errors)
    """
    total = await count_total(session, TopologyEmbeddingModel)
    null_count = await count_null_embeddings(
        session, TopologyEmbeddingModel, TopologyEmbeddingModel.embedding
    )
    has_embedding = total - null_count

    print("\n  Topology Entities:")
    print(f"    Total: {total}")
    print(f"    With embeddings: {has_embedding}")
    print(f"    Need re-embedding: {null_count}")

    if null_count == 0:
        print("    All entities already have embeddings.")
        return 0, 0

    if dry_run:
        print("    [DRY RUN] No changes will be made.")
        return 0, 0

    processed = 0
    errors = 0
    offset = 0

    while True:
        # Join with entity to get description text
        stmt = (
            select(TopologyEmbeddingModel, TopologyEntityModel.description)
            .join(TopologyEntityModel, TopologyEmbeddingModel.entity_id == TopologyEntityModel.id)
            .where(TopologyEmbeddingModel.embedding.is_(None))
            .order_by(TopologyEmbeddingModel.entity_id)
            .offset(offset)
            .limit(batch_size)
        )
        result = await session.execute(stmt)
        rows = list(result.all())

        if not rows:
            break

        embedding_models = [row[0] for row in rows]
        descriptions = [row[1] for row in rows]

        try:
            new_embeddings = await embeddings.embed_batch(descriptions, input_type="document")

            for emb_model, embedding in zip(embedding_models, new_embeddings):
                emb_model.embedding = embedding

            await session.commit()
            processed += len(rows)
            print(f"    Processed {processed}/{null_count} entities "
                  f"({processed / null_count * 100:.1f}%)")

        except Exception as e:
            logger.error("Batch failed", error=str(e))
            errors += len(rows)
            await session.rollback()
            print(f"    ERROR: Skipping {len(rows)} entities: {e}")

        offset += batch_size

    return processed, errors


async def main(
    batch_size: int = 100,
    dry_run: bool = False,
    knowledge_only: bool = False,
    topology_only: bool = False,
):
    """Re-embed all data with Voyage AI."""
    config = get_config()

    print("=" * 60)
    print("Voyage AI Re-Embedding Script")
    print("=" * 60)

    # Pre-flight validation: DB, API key, migrations
    await preflight_check()
    print("\n  Configuration:")
    print(f"    Embedding Model: {config.embedding_model}")
    print(f"    Voyage API Key: {'Set' if config.voyage_api_key else 'MISSING'}")
    print(f"    Batch Size: {batch_size}")
    print(f"    Dry Run: {dry_run}")
    print(f"    Scope: {'knowledge only' if knowledge_only else 'topology only' if topology_only else 'all'}")

    if not config.voyage_api_key:
        print("\n  ERROR: VOYAGE_API_KEY not set!")
        sys.exit(1)

    # Initialize Voyage AI embedding provider
    embeddings = VoyageAIEmbeddings(
        api_key=config.voyage_api_key,
        model=config.embedding_model,
    )
    print("\n  Provider: VoyageAIEmbeddings")
    print(f"    Model: {embeddings.model}")
    print(f"    Dimensions: {embeddings.dimension}")

    session_maker = get_session_maker()
    start_time = datetime.now()

    total_processed = 0
    total_errors = 0

    async with session_maker() as session:
        if not topology_only:
            processed, errs = await re_embed_knowledge_chunks(
                session, embeddings, batch_size, dry_run
            )
            total_processed += processed
            total_errors += errs

        if not knowledge_only:
            processed, errs = await re_embed_topology_entities(
                session, embeddings, batch_size, dry_run
            )
            total_processed += processed
            total_errors += errs

    elapsed = (datetime.now() - start_time).total_seconds()

    print("\n" + "=" * 60)
    print("Results:")
    print(f"  Total re-embedded: {total_processed}")
    print(f"  Total errors: {total_errors}")
    print(f"  Duration: {elapsed:.1f}s")
    if total_processed > 0 and elapsed > 0:
        print(f"  Rate: {total_processed / elapsed:.1f} items/sec")
    print("=" * 60)


def cli():
    parser = argparse.ArgumentParser(
        description="Re-embed knowledge chunks and topology entities with Voyage AI"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Number of items to process per batch (default: 100, max: 1000)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count items without making changes",
    )
    parser.add_argument(
        "--knowledge-only",
        action="store_true",
        help="Only re-embed knowledge chunks",
    )
    parser.add_argument(
        "--topology-only",
        action="store_true",
        help="Only re-embed topology entities",
    )

    args = parser.parse_args()

    if args.knowledge_only and args.topology_only:
        parser.error("Cannot specify both --knowledge-only and --topology-only")

    asyncio.run(main(
        batch_size=min(args.batch_size, 1000),
        dry_run=args.dry_run,
        knowledge_only=args.knowledge_only,
        topology_only=args.topology_only,
    ))


if __name__ == "__main__":
    cli()
