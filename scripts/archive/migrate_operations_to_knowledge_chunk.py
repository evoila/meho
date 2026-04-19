#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Migrate existing connector operations to knowledge_chunk table.

TASK-126: Unified Connector Search Architecture

This script creates knowledge_chunk entries with embeddings for existing:
- VMware connector operations (from connector_operation table)
- SOAP operations (if any exist)

This enables hybrid search (BM25 + semantic) for all connector types.

Usage:
    python scripts/migrate_operations_to_knowledge_chunk.py [--dry-run] [--connector-type vmware|soap|all]

Prerequisites:
    1. Ensure OPENAI_API_KEY is set in environment
    2. Database migrations are up to date
    3. Connectors and operations already exist in database

Performance:
    - ~100 operations/minute (due to embedding generation rate limits)
    - Typical VMware connector: ~50 operations = ~30 seconds
"""
import asyncio
import sys
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime
import argparse

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

logger = structlog.get_logger(__name__)


async def count_vmware_operations(session: AsyncSession) -> int:
    """Count existing VMware connector operations."""
    from meho_openapi.models import ConnectorOperationModel
    
    stmt = select(func.count()).select_from(ConnectorOperationModel)
    result = await session.execute(stmt)
    return result.scalar() or 0


async def count_existing_operation_chunks(session: AsyncSession) -> int:
    """Count existing knowledge chunks for connector operations."""
    from meho_knowledge.models import KnowledgeChunkModel
    
    stmt = select(func.count()).select_from(KnowledgeChunkModel).where(
        KnowledgeChunkModel.search_metadata["source_type"].astext == "connector_operation"
    )
    result = await session.execute(stmt)
    return result.scalar() or 0


async def get_vmware_connectors(session: AsyncSession) -> List[Dict[str, Any]]:
    """Get all VMware connectors."""
    from meho_openapi.models import ConnectorModel
    
    stmt = select(ConnectorModel).where(
        ConnectorModel.connector_type == "vmware"
    )
    result = await session.execute(stmt)
    connectors = result.scalars().all()
    
    return [
        {
            "id": str(c.id),
            "name": c.name,
            "tenant_id": str(c.tenant_id),
        }
        for c in connectors
    ]


async def migrate_vmware_connector(
    session: AsyncSession,
    connector: Dict[str, Any],
    knowledge_store: Any,
    dry_run: bool = False
) -> int:
    """
    Migrate a single VMware connector's operations to knowledge_chunk.
    
    Args:
        session: Database session
        connector: Connector info dict
        knowledge_store: KnowledgeStore instance
        dry_run: If True, only count without making changes
        
    Returns:
        Number of chunks created
    """
    from meho_openapi.connectors.vmware.operations import VMWARE_OPERATIONS
    from meho_openapi.connectors.vmware.sync import (
        _format_vmware_operation_as_text,
        _sync_vmware_knowledge_chunks,
    )
    
    connector_id = connector["id"]
    connector_name = connector["name"]
    tenant_id = connector["tenant_id"]
    
    print(f"  📦 Processing: {connector_name} ({connector_id})")
    print(f"     Operations: {len(VMWARE_OPERATIONS)}")
    
    if dry_run:
        print(f"     [DRY RUN] Would create {len(VMWARE_OPERATIONS)} chunks")
        return len(VMWARE_OPERATIONS)
    
    # Use the sync function which handles chunk creation
    chunks_created = await _sync_vmware_knowledge_chunks(
        knowledge_store=knowledge_store,
        connector_id=connector_id,
        connector_name=connector_name,
        tenant_id=tenant_id,
    )
    
    print(f"     ✅ Created {chunks_created} knowledge chunks")
    return chunks_created


async def migrate_soap_connectors(
    session: AsyncSession,
    knowledge_store: Any,
    dry_run: bool = False
) -> int:
    """
    Migrate SOAP connector operations to knowledge_chunk.
    
    Note: SOAP operations are parsed on-demand from WSDL, so this
    function queries existing SOAP operation descriptors.
    
    Returns:
        Number of chunks created
    """
    from meho_openapi.models import SoapOperationDescriptorModel
    from meho_openapi.models import ConnectorModel
    
    # Get all SOAP connectors
    stmt = select(ConnectorModel).where(
        ConnectorModel.connector_type == "soap"
    )
    result = await session.execute(stmt)
    connectors = result.scalars().all()
    
    if not connectors:
        print("  No SOAP connectors found")
        return 0
    
    total_chunks = 0
    
    for connector in connectors:
        print(f"  📦 Processing SOAP connector: {connector.name}")
        
        # Get SOAP operations for this connector
        # Note: These are stored in soap_operation_descriptor table
        try:
            from meho_openapi.repository import SoapOperationRepository
            
            soap_repo = SoapOperationRepository(session)
            operations = await soap_repo.list_operations(
                connector_id=str(connector.id),
                limit=1000
            )
            
            print(f"     Found {len(operations)} SOAP operations")
            
            if dry_run:
                print(f"     [DRY RUN] Would create {len(operations)} chunks")
                total_chunks += len(operations)
                continue
            
            # Create knowledge chunks for each operation
            from meho_knowledge.schemas import KnowledgeChunkCreate, KnowledgeType
            from meho_knowledge.models import KnowledgeChunkModel
            
            # Delete existing chunks for this connector
            delete_stmt = delete(KnowledgeChunkModel).where(
                KnowledgeChunkModel.tenant_id == str(connector.tenant_id),
                KnowledgeChunkModel.search_metadata["connector_id"].astext == str(connector.id),
                KnowledgeChunkModel.search_metadata["source_type"].astext == "connector_operation",
                KnowledgeChunkModel.search_metadata["connector_type"].astext == "soap",
            )
            await session.execute(delete_stmt)
            
            for op in operations:
                text = _format_soap_operation_as_text(op, connector.name)
                
                chunk_create = KnowledgeChunkCreate(
                    text=text,
                    tenant_id=str(connector.tenant_id),
                    tags=["api", "operation", "soap", op.service_name or "", op.port_name or ""],
                    knowledge_type=KnowledgeType.DOCUMENTATION,
                    priority=5,
                    search_metadata={
                        "source_type": "connector_operation",
                        "connector_id": str(connector.id),
                        "connector_type": "soap",
                        "operation_name": op.operation_name,
                        "service_name": op.service_name,
                        "port_name": op.port_name,
                    },
                    source_uri=f"connector://{connector.id}/operation/{op.operation_name}"
                )
                
                await knowledge_store.add_chunk(chunk_create)
                total_chunks += 1
            
            print(f"     ✅ Created {len(operations)} knowledge chunks")
            
        except Exception as e:
            print(f"     ❌ Error processing SOAP connector: {e}")
            continue
    
    return total_chunks


def _format_soap_operation_as_text(op: Any, connector_name: str) -> str:
    """Format SOAP operation as searchable text."""
    parts = [
        f"{op.operation_name}",
        "",
        f"Service: {op.service_name or 'Unknown'}",
        f"Port: {op.port_name or 'Unknown'}",
        f"Connector: {connector_name}",
        "",
    ]
    
    if op.description:
        parts.append(f"Description: {op.description}")
        parts.append("")
    
    parts.append("Search: soap web service operation")
    
    return "\n".join(parts)


async def run_migration(
    connector_type: str = "all",
    dry_run: bool = False
):
    """
    Run the migration.
    
    Args:
        connector_type: "vmware", "soap", or "all"
        dry_run: If True, only report what would be done
    """
    from meho_knowledge.database import get_session_maker
    from meho_knowledge.repository import KnowledgeRepository
    from meho_knowledge.embeddings import OpenAIEmbeddings
    from meho_knowledge.knowledge_store import KnowledgeStore
    from meho_core.config import get_config
    
    config = get_config()
    
    print("=" * 60)
    print("TASK-126: Migrate Connector Operations to Knowledge Chunks")
    print("=" * 60)
    print()
    print(f"📋 Configuration:")
    print(f"  Connector Type: {connector_type}")
    print(f"  Embedding Model: {config.embedding_model}")
    print(f"  Dry Run: {dry_run}")
    print()
    
    if not config.openai_api_key:
        print("❌ ERROR: OPENAI_API_KEY not set!")
        sys.exit(1)
    
    session_maker = get_session_maker()
    
    async with session_maker() as session:
        # Count existing data
        op_count = await count_vmware_operations(session)
        existing_chunks = await count_existing_operation_chunks(session)
        
        print(f"📊 Current State:")
        print(f"  VMware operations in DB: {op_count}")
        print(f"  Existing operation chunks: {existing_chunks}")
        print()
        
        if dry_run:
            print("🔍 [DRY RUN] Analyzing what would be done...")
            print()
        
        # Create knowledge store
        repo = KnowledgeRepository(session)
        embeddings = OpenAIEmbeddings(
            api_key=config.openai_api_key,
            model=config.embedding_model
        )
        knowledge_store = KnowledgeStore(repo, embeddings)
        
        total_chunks = 0
        
        # Migrate VMware connectors
        if connector_type in ("vmware", "all"):
            print("🔧 Migrating VMware connectors...")
            vmware_connectors = await get_vmware_connectors(session)
            print(f"  Found {len(vmware_connectors)} VMware connector(s)")
            
            for connector in vmware_connectors:
                chunks = await migrate_vmware_connector(
                    session, connector, knowledge_store, dry_run
                )
                total_chunks += chunks
            
            print()
        
        # Migrate SOAP connectors
        if connector_type in ("soap", "all"):
            print("🔧 Migrating SOAP connectors...")
            chunks = await migrate_soap_connectors(session, knowledge_store, dry_run)
            total_chunks += chunks
            print()
        
        # Commit if not dry run
        if not dry_run:
            await session.commit()
            print(f"✅ Migration complete! Created {total_chunks} knowledge chunks.")
        else:
            print(f"🔍 [DRY RUN] Would create {total_chunks} knowledge chunks.")
        
        print()
        print("=" * 60)
        print("Next Steps:")
        print("  1. Set endpoint_search_algorithm='bm25_hybrid' in config")
        print("  2. Restart the API service")
        print("  3. Test search with queries like 'list VMs' or 'show health'")
        print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="Migrate connector operations to knowledge_chunk for hybrid search"
    )
    parser.add_argument(
        "--connector-type",
        choices=["vmware", "soap", "all"],
        default="all",
        help="Type of connectors to migrate (default: all)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only report what would be done without making changes"
    )
    
    args = parser.parse_args()
    
    asyncio.run(run_migration(
        connector_type=args.connector_type,
        dry_run=args.dry_run
    ))


if __name__ == "__main__":
    main()

