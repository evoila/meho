#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Run knowledge cleanup job.

Removes expired event chunks to prevent knowledge base bloat.

Usage:
  # Run once
  python scripts/run-cleanup.py
  
  # Run as cron (daily at 2 AM)
  0 2 * * * cd /path/to/MEHO.X && python scripts/run-cleanup.py

Returns:
  Exit code 0 on success, 1 on failure
"""
import asyncio
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from meho_knowledge.database import get_async_session
from meho_knowledge.deps import get_vector_store
from meho_knowledge.cleanup import cleanup_expired_events, get_cleanup_statistics
import logging

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def main():
    """Run cleanup job"""
    try:
        logger.info("=" * 60)
        logger.info("Starting knowledge cleanup job")
        logger.info("=" * 60)
        
        # Get dependencies
        vector_store = get_vector_store()
        
        async with get_async_session() as session:
            # Get statistics before cleanup
            logger.info("\n📊 Statistics BEFORE cleanup:")
            before_stats = await get_cleanup_statistics(session)
            logger.info(f"  Total chunks: {before_stats['total_chunks']}")
            logger.info(f"  By type: {before_stats['chunks_by_type']}")
            logger.info(f"  Expired (not deleted): {before_stats['expired_not_deleted']}")
            logger.info(f"  Expiring in 24h: {before_stats['expiring_in_24h']}")
            
            # Run cleanup
            logger.info("\n🧹 Running cleanup...")
            cleanup_result = await cleanup_expired_events(session, vector_store)
            
            logger.info("\n✅ Cleanup Results:")
            logger.info(f"  Database: {cleanup_result['deleted_count']} chunks deleted")
            logger.info(f"  Vector store: {cleanup_result['vector_deleted_count']} chunks deleted")
            logger.info(f"  Timestamp: {cleanup_result['timestamp']}")
            
            # Get statistics after cleanup
            logger.info("\n📊 Statistics AFTER cleanup:")
            after_stats = await get_cleanup_statistics(session)
            logger.info(f"  Total chunks: {after_stats['total_chunks']}")
            logger.info(f"  By type: {after_stats['chunks_by_type']}")
            logger.info(f"  Expired (not deleted): {after_stats['expired_not_deleted']}")
            
            logger.info("\n" + "=" * 60)
            logger.info("✅ Cleanup job completed successfully!")
            logger.info("=" * 60)
            
            return 0
            
    except Exception as e:
        logger.error(f"\n❌ Cleanup job failed: {type(e).__name__}: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)

