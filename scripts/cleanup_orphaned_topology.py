#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Cleanup Orphaned Topology Entities

Removes topology entities whose connector_id references connectors that no longer exist.
This is a one-time cleanup script to fix duplicates caused by connectors being deleted
and re-created before cascade delete was implemented.

Usage:
    # Dry run (shows what would be deleted without actually deleting):
    python scripts/cleanup_orphaned_topology.py --dry-run
    
    # Actually delete orphaned entities:
    python scripts/cleanup_orphaned_topology.py
    
    # Run from Docker:
    docker exec meho-meho-api-1 python scripts/cleanup_orphaned_topology.py --dry-run
"""
import asyncio
import sys
import argparse
from pathlib import Path
from uuid import UUID

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


async def get_valid_connector_ids(session) -> list[UUID]:
    """Get list of connector IDs that currently exist."""
    from sqlalchemy import select
    from meho_app.modules.connectors.models import ConnectorModel
    
    result = await session.execute(select(ConnectorModel.id))
    return [row[0] for row in result]


async def get_orphaned_entities(session, valid_connector_ids: list[UUID]) -> list[dict]:
    """Get topology entities whose connector_id is not in valid connectors."""
    from sqlalchemy import select, and_
    from meho_app.modules.topology.models import TopologyEntityModel
    
    # Get entities with connector_id not in valid list
    if not valid_connector_ids:
        # If no connectors exist, all entities with a connector_id are orphaned
        query = select(TopologyEntityModel).where(
            TopologyEntityModel.connector_id.isnot(None)
        )
    else:
        query = select(TopologyEntityModel).where(
            and_(
                TopologyEntityModel.connector_id.isnot(None),
                TopologyEntityModel.connector_id.not_in(valid_connector_ids),
            )
        )
    
    result = await session.execute(query)
    entities = result.scalars().all()
    
    return [
        {
            "id": str(e.id),
            "name": e.name,
            "entity_type": e.entity_type,
            "connector_id": str(e.connector_id),
            "tenant_id": e.tenant_id,
            "discovered_at": e.discovered_at.isoformat() if e.discovered_at else None,
        }
        for e in entities
    ]


async def cleanup_orphaned_topology(dry_run: bool = True):
    """Main cleanup function."""
    from meho_app.database import get_session_maker
    from meho_app.modules.topology.service import TopologyService
    
    print(f"\n{'='*80}")
    print(f"🧹 TOPOLOGY ORPHAN CLEANUP {'(DRY RUN)' if dry_run else ''}")
    print(f"{'='*80}\n")
    
    session_maker = get_session_maker()
    
    async with session_maker() as session:
        # Get valid connector IDs
        valid_connector_ids = await get_valid_connector_ids(session)
        print(f"📋 Found {len(valid_connector_ids)} active connectors\n")
        
        # Get orphaned entities
        orphaned = await get_orphaned_entities(session, valid_connector_ids)
        
        if not orphaned:
            print("✅ No orphaned topology entities found!")
            return 0
        
        print(f"⚠️  Found {len(orphaned)} orphaned topology entities:\n")
        
        # Group by connector_id for better output
        by_connector: dict[str, list[dict]] = {}
        for entity in orphaned:
            cid = entity["connector_id"]
            if cid not in by_connector:
                by_connector[cid] = []
            by_connector[cid].append(entity)
        
        for connector_id, entities in by_connector.items():
            print(f"  Connector {connector_id[:8]}... ({len(entities)} entities):")
            for e in entities[:5]:  # Show first 5
                print(f"    - {e['name']} ({e['entity_type']})")
            if len(entities) > 5:
                print(f"    - ... and {len(entities) - 5} more")
            print()
        
        if dry_run:
            print(f"{'='*80}")
            print("🔍 DRY RUN: No changes made")
            print(f"   Run without --dry-run to delete {len(orphaned)} entities")
            print(f"{'='*80}\n")
            return len(orphaned)
        
        # Actually delete
        print("Deleting orphaned entities...")
        topology_service = TopologyService(session)
        count = await topology_service.cleanup_orphaned_entities(valid_connector_ids)
        
        print(f"\n{'='*80}")
        print(f"✅ CLEANUP COMPLETE: Deleted {count} orphaned topology entities")
        print(f"{'='*80}\n")
        
        return count


def main():
    parser = argparse.ArgumentParser(
        description="Cleanup orphaned topology entities",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Preview what would be deleted:
    python scripts/cleanup_orphaned_topology.py --dry-run
    
    # Actually delete orphaned entities:
    python scripts/cleanup_orphaned_topology.py
        """
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without actually deleting"
    )
    
    args = parser.parse_args()
    
    count = asyncio.run(cleanup_orphaned_topology(dry_run=args.dry_run))
    
    # Exit with non-zero if orphans found in dry-run (useful for CI)
    if args.dry_run and count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()

