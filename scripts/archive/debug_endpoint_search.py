#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Debug Endpoint Search

Helps diagnose why semantic search is returning wrong endpoints.
Shows what knowledge chunks exist for a connector and how they match queries.

Usage:
    python scripts/debug_endpoint_search.py <connector_id> "search query"
    
Example:
    python scripts/debug_endpoint_search.py a72f87bf-fddb-450b-8d96-b18d6b5b0c08 "list virtual machines"
"""
import asyncio
import sys
import os
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from meho_knowledge.knowledge_store import KnowledgeStore
from meho_knowledge.repository import KnowledgeRepository
from meho_knowledge.embeddings import EmbeddingProvider
from meho_knowledge.hybrid_search import PostgresFTSHybridService
from meho_openapi.repository import EndpointDescriptorRepository
from meho_knowledge.database import create_knowledge_session_maker
from meho_openapi.database import create_openapi_session_maker
from meho_core.auth_context import UserContext
from meho_core.config import get_settings


async def debug_endpoint_search(connector_id: str, query: str):
    """Debug why endpoint search returns certain results"""
    settings = get_settings()
    
    # Create user context
    user = UserContext(
        user_id="debug-user",
        tenant_id="demo-tenant",
        roles=["admin"]
    )
    
    # Create database sessions
    knowledge_session_maker = create_knowledge_session_maker(settings.database_url)
    openapi_session_maker = create_openapi_session_maker(settings.database_url)
    
    async with knowledge_session_maker() as knowledge_session:
        async with openapi_session_maker() as openapi_session:
            # Create knowledge store
            knowledge_repo = KnowledgeRepository(knowledge_session)
            embeddings = EmbeddingProvider(settings)
            hybrid_search = PostgresFTSHybridService(knowledge_repo, embeddings)
            knowledge_store = KnowledgeStore(knowledge_repo, embeddings, hybrid_search)
            
            # Create endpoint repository
            endpoint_repo = EndpointDescriptorRepository(openapi_session)
            
            print(f"\n{'='*80}")
            print(f"🔍 DEBUGGING ENDPOINT SEARCH")
            print(f"{'='*80}")
            print(f"Connector ID: {connector_id}")
            print(f"Query: '{query}'")
            print(f"{'='*80}\n")
            
            # Step 1: Show all endpoints for this connector
            print("📋 Step 1: All Endpoints in Connector")
            print("-" * 80)
            
            from meho_openapi.schemas import EndpointFilter
            all_endpoints = await endpoint_repo.list_endpoints(
                EndpointFilter(connector_id=connector_id, is_enabled=True, limit=1000)
            )
            
            print(f"Total endpoints: {len(all_endpoints)}\n")
            
            # Group by resource type
            by_path_prefix = {}
            for ep in all_endpoints:
                # Extract first non-variable path part after /api or /rest
                parts = [p for p in ep.path.split('/') if p and not p.startswith('{')]
                if len(parts) >= 2:
                    prefix = f"/{parts[0]}/{parts[1]}" if len(parts) > 1 else f"/{parts[0]}"
                else:
                    prefix = "/misc"
                
                if prefix not in by_path_prefix:
                    by_path_prefix[prefix] = []
                by_path_prefix[prefix].append(ep)
            
            print("Endpoints by path prefix:")
            for prefix in sorted(by_path_prefix.keys())[:20]:  # Show first 20 prefixes
                eps = by_path_prefix[prefix]
                print(f"  {prefix}: {len(eps)} endpoints")
                # Show first 3 examples
                for ep in eps[:3]:
                    print(f"    - {ep.method:6s} {ep.path}")
                    if ep.summary:
                        print(f"      → {ep.summary[:80]}")
            
            if len(by_path_prefix) > 20:
                print(f"  ... and {len(by_path_prefix) - 20} more prefixes")
            
            # Step 2: Search knowledge base
            print(f"\n📚 Step 2: Knowledge Base Search")
            print("-" * 80)
            print(f"Searching for: '{query}'")
            print(f"Filters: source_type=openapi_spec, connector_id={connector_id}\n")
            
            search_results = await knowledge_store.search_hybrid(
                query=query,
                user_context=user,
                top_k=20,
                score_threshold=0.3,
                metadata_filters={
                    "source_type": "openapi_spec",
                    "connector_id": connector_id
                }
            )
            
            print(f"Found {len(search_results)} knowledge chunks\n")
            
            if search_results:
                print("Top 10 Results:")
                for i, chunk in enumerate(search_results[:10]):
                    print(f"\n{i+1}. Chunk ID: {chunk.id}")
                    print(f"   Text preview: {chunk.text[:200]}...")
                    
                    if chunk.search_metadata:
                        metadata = chunk.search_metadata.model_dump() if hasattr(chunk.search_metadata, 'model_dump') else chunk.search_metadata
                        print(f"   Metadata:")
                        print(f"     - operation_id: {metadata.get('operation_id')}")
                        print(f"     - endpoint_path: {metadata.get('endpoint_path')}")
                        print(f"     - http_method: {metadata.get('http_method')}")
                        print(f"     - resource_type: {metadata.get('resource_type')}")
                        print(f"     - keywords: {metadata.get('keywords')}")
            else:
                print("❌ No knowledge chunks found!")
                print("\nPossible causes:")
                print("  1. OpenAPI spec not ingested for this connector")
                print("  2. Connector ID mismatch")
                print("  3. Embeddings not generated")
                print("\nTry re-uploading the OpenAPI spec for this connector.")
            
            # Step 3: Match with actual endpoints
            if search_results:
                print(f"\n🔗 Step 3: Matching Knowledge Chunks to Endpoints")
                print("-" * 80)
                
                matched_count = 0
                for i, chunk in enumerate(search_results[:10]):
                    if chunk.search_metadata:
                        metadata = chunk.search_metadata.model_dump() if hasattr(chunk.search_metadata, 'model_dump') else chunk.search_metadata
                        operation_id = metadata.get('operation_id')
                        endpoint_path = metadata.get('endpoint_path')
                        http_method = metadata.get('http_method')
                        
                        # Find matching endpoint
                        matched = None
                        for ep in all_endpoints:
                            if operation_id and ep.operation_id == operation_id:
                                matched = ep
                                break
                            elif endpoint_path == ep.path and http_method == ep.method:
                                matched = ep
                                break
                        
                        if matched:
                            matched_count += 1
                            print(f"\n✅ Chunk {i+1} → {matched.method} {matched.path}")
                            if matched.summary:
                                print(f"   Summary: {matched.summary}")
                        else:
                            print(f"\n❌ Chunk {i+1} → No matching endpoint found!")
                            print(f"   Expected: {http_method} {endpoint_path}")
                
                print(f"\n📊 Matched {matched_count}/{len(search_results[:10])} chunks to actual endpoints")
            
            # Step 4: Recommendations
            print(f"\n💡 Recommendations")
            print("-" * 80)
            
            if not search_results:
                print("❌ No knowledge chunks found for this connector")
                print("\nAction items:")
                print("  1. Re-upload the OpenAPI spec")
                print("  2. Verify connector_id is correct")
                print("  3. Check if spec ingestion completed successfully")
            elif matched_count < len(search_results[:10]) * 0.5:
                print("⚠️  Many knowledge chunks don't match actual endpoints")
                print("\nAction items:")
                print("  1. Re-ingest the OpenAPI spec")
                print("  2. Check for data consistency issues")
            else:
                print("✅ Knowledge base search is working")
                print("\nBut results don't match the query. Possible improvements:")
                print("  1. Add custom descriptions to endpoints")
                print("  2. Improve OpenAPI spec summaries")
                print("  3. Add more keywords/tags to endpoint metadata")
                print("  4. Try different search queries (e.g., 'vm inventory', 'managed objects')")
            
            print(f"\n{'='*80}")
            print("Debug complete!")
            print(f"{'='*80}\n")


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    
    connector_id = sys.argv[1]
    query = sys.argv[2]
    
    asyncio.run(debug_endpoint_search(connector_id, query))


if __name__ == "__main__":
    main()

