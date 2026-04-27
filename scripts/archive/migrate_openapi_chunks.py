#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Migration script to regenerate OpenAPI knowledge chunks with enhanced search keywords.

Session 80: Adds search keywords (abbreviations, synonyms, path patterns) to chunks
to improve BM25 matching for queries like "list VMs" → "virtual machines".

Usage:
    docker compose -f docker-compose.dev.yml exec meho-api python3 scripts/migrate_openapi_chunks.py

This will:
1. Fetch all OpenAPI specs from the database
2. Delete existing knowledge chunks for each connector
3. Re-ingest with enhanced chunking (includes search keywords)
4. Preserve all connector and endpoint data (only chunks are regenerated)
"""
import asyncio
import sys
import yaml
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

# Add project root to path
sys.path.insert(0, '/app')

from meho_knowledge.database import get_session_maker
from meho_knowledge.knowledge_store import KnowledgeStore
from meho_knowledge.repository import KnowledgeRepository
from meho_knowledge.embeddings import OpenAIEmbeddings
    from meho_knowledge.hybrid_search import PostgresFTSHybridService
from meho_openapi.models import OpenAPISpecModel, ConnectorModel
from meho_openapi.knowledge_ingestion import ingest_openapi_to_knowledge, remove_connector_knowledge
from meho_core.auth_context import UserContext
import os


async def migrate_all_chunks():
    """Regenerate all OpenAPI knowledge chunks with enhanced search keywords."""
    
    print("=" * 100)
    print("OpenAPI Knowledge Chunk Migration - Session 80")
    print("=" * 100)
    print()
    print("This will regenerate all OpenAPI knowledge chunks with:")
    print("  ✅ Search keywords (VM → vm, vms, virtualmachine, list-vm, etc.)")
    print("  ✅ Abbreviation handling")
    print("  ✅ Path pattern matching")
    print()
    print("⚠️  This operation will:")
    print("  - Delete existing OpenAPI chunks")
    print("  - Regenerate from stored specs")
    print("  - Preserve all connector and endpoint data")
    print()
    input("Press Enter to continue or Ctrl+C to cancel...")
    print()
    
    # Create database session
    session_maker = get_session_maker()
    
    async with session_maker() as session:
        # Create knowledge store
        openai_api_key = os.getenv("OPENAI_API_KEY")
        if not openai_api_key:
            print("❌ OPENAI_API_KEY environment variable required!")
            return
        
        repository = KnowledgeRepository(session)
        embedding_provider = OpenAIEmbeddings(api_key=openai_api_key, model="text-embedding-3-small")
        hybrid_search = PostgresFTSHybridService(repository, embedding_provider)
        knowledge_store = KnowledgeStore(
            repository=repository,
            embedding_provider=embedding_provider,
            hybrid_search_service=hybrid_search
        )
        
        # Fetch all OpenAPI specs with their connectors
        stmt = (
            select(OpenAPISpecModel, ConnectorModel)
            .join(ConnectorModel, OpenAPISpecModel.connector_id == ConnectorModel.id)
            .where(ConnectorModel.is_active == True)
        )
        
        result = await session.execute(stmt)
        spec_connector_pairs = result.all()
        
        if not spec_connector_pairs:
            print("⚠️  No active connectors with OpenAPI specs found!")
            return
        
        print(f"📋 Found {len(spec_connector_pairs)} connectors with OpenAPI specs")
        print()
        
        total_regenerated = 0
        
        for spec_model, connector_model in spec_connector_pairs:
            connector_id = str(connector_model.id)
            connector_name = connector_model.name
            tenant_id = connector_model.tenant_id
            
            print(f"🔄 Processing: {connector_name}")
            print(f"   Connector ID: {connector_id[:12]}...")
            print(f"   Tenant: {tenant_id}")
            
            try:
                # Parse the spec
                spec_dict = yaml.safe_load(spec_model.spec_content)
                
                # Create user context for this tenant
                user_context = UserContext(
                    user_id="migration-script",
                    tenant_id=tenant_id,
                    is_admin=True
                )
                
                # Step 1: Delete old chunks
                print(f"   🗑️  Deleting old chunks...")
                deleted_count = await remove_connector_knowledge(
                    connector_id=connector_id,
                    knowledge_store=knowledge_store,
                    user_context=user_context
                )
                print(f"   ✅ Deleted {deleted_count} old chunks")
                
                # Step 2: Re-ingest with new format
                print(f"   ➕ Re-ingesting with enhanced keywords...")
                created_count = await ingest_openapi_to_knowledge(
                    spec_dict=spec_dict,
                    connector_id=connector_id,
                    connector_name=connector_name,
                    knowledge_store=knowledge_store,
                    user_context=user_context
                )
                print(f"   ✅ Created {created_count} new chunks")
                
                total_regenerated += created_count
                print()
                
            except Exception as e:
                print(f"   ❌ Error: {e}")
                print()
                continue
        
        # Commit all changes
        await session.commit()
        
        print("=" * 100)
        print(f"✅ Migration Complete!")
        print(f"   Total chunks regenerated: {total_regenerated}")
        print(f"   Connectors processed: {len(spec_connector_pairs)}")
        print("=" * 100)


if __name__ == "__main__":
    try:
        asyncio.run(migrate_all_chunks())
    except KeyboardInterrupt:
        print("\n⚠️  Migration cancelled by user")
        sys.exit(1)

