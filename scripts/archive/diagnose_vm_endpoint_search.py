#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Diagnostic script to find why VM endpoint isn't being returned by BM25 search.

Run this against your running MEHO instance to diagnose the issue.

Usage:
    python scripts/diagnose_vm_endpoint_search.py
"""
import asyncio
import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select, text
from uuid import UUID
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from meho_knowledge.models import KnowledgeChunkModel
from meho_knowledge.bm25_service import BM25Service


async def main():
    print("🔍 MEHO VM Endpoint Search Diagnostic")
    print("=" * 80)
    
    # Get database URL from environment
    db_url = os.getenv("MEHO_KNOWLEDGE_DATABASE_URL", "postgresql+asyncpg://meho:meho@localhost:5432/meho_knowledge")
    print(f"\n📊 Connecting to: {db_url.split('@')[1] if '@' in db_url else db_url}")
    
    # vCenter connector ID from your logfire trace
    connector_id = "a72f87bf-fddb-450b-8d96-b18d6b5b0c08"
    
    # Create async engine
    engine = create_async_engine(db_url, echo=False)
    async_session_maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    
    try:
        async with async_session_maker() as session:
            print(f"\n✅ Connected to database!")
            
            # Step 1: Check total endpoint count for this connector
            print(f"\n" + "=" * 80)
            print(f"1️⃣  CHECKING ENDPOINT COUNT")
            print("=" * 80)
            
            stmt = select(KnowledgeChunkModel).where(
                KnowledgeChunkModel.search_metadata["connector_id"].astext == connector_id,
                KnowledgeChunkModel.search_metadata["source_type"].astext == "openapi_spec"
            )
            
            result = await session.execute(stmt)
            all_endpoints = result.scalars().all()
            
            print(f"\n📦 Found {len(all_endpoints)} total endpoints in knowledge base")
            
            if len(all_endpoints) == 0:
                print("\n❌ ERROR: No endpoints found for this connector!")
                print("   This means the OpenAPI spec was never ingested.")
                print("\n💡 Action needed:")
                print("   1. Upload the vCenter OpenAPI spec via the UI")
                print("   2. Or use POST /api/connectors/{connector_id}/openapi-spec")
                return
            
            # Step 2: Look for VM endpoint specifically
            print(f"\n" + "=" * 80)
            print(f"2️⃣  SEARCHING FOR VM ENDPOINT")
            print("=" * 80)
            
            vm_endpoints = []
            for endpoint in all_endpoints:
                if not endpoint.search_metadata:
                    continue
                    
                path = endpoint.search_metadata.get('endpoint_path', '')
                if 'vm' in path.lower():
                    vm_endpoints.append(endpoint)
            
            print(f"\n🔍 Found {len(vm_endpoints)} endpoint(s) with 'vm' in path:")
            
            if vm_endpoints:
                for ep in vm_endpoints:
                    path = ep.search_metadata.get('endpoint_path')
                    method = ep.search_metadata.get('http_method')
                    operation_id = ep.search_metadata.get('operation_id', 'N/A')
                    print(f"\n   📍 {method} {path}")
                    print(f"      Operation ID: {operation_id}")
                    print(f"      Text preview: {ep.text[:150]}...")
            else:
                print("\n   ❌ No endpoints with 'vm' in path found!")
            
            # Step 3: List first 20 endpoints to see what IS there
            print(f"\n" + "=" * 80)
            print(f"3️⃣  FIRST 20 ENDPOINTS IN KNOWLEDGE BASE")
            print("=" * 80)
            
            print("\n📋 Here's what endpoints ARE available:\n")
            for i, ep in enumerate(all_endpoints[:20], 1):
                path = ep.search_metadata.get('endpoint_path', 'unknown') if ep.search_metadata else 'unknown'
                method = ep.search_metadata.get('http_method', '?') if ep.search_metadata else '?'
                print(f"   {i:2}. {method:6} {path}")
            
            if len(all_endpoints) > 20:
                print(f"\n   ... and {len(all_endpoints) - 20} more endpoints")
            
            # Step 4: Test BM25 search with actual query
            print(f"\n" + "=" * 80)
            print(f"4️⃣  TESTING BM25 SEARCH")
            print("=" * 80)
            
            print(f"\n🔎 Query: \"list virtual machines\"")
            print(f"   Connector: {connector_id}")
            
            # Get tenant_id from first endpoint
            tenant_id = all_endpoints[0].tenant_id if all_endpoints else "test-tenant"
            
            bm25_service = BM25Service(session)
            
            results = await bm25_service.search(
                tenant_id=UUID(tenant_id),
                query="list virtual machines",
                metadata_filters={
                    "source_type": "openapi_spec",
                    "connector_id": connector_id
                },
                top_k=15
            )
            
            print(f"\n📊 BM25 returned {len(results)} results:\n")
            
            for i, result in enumerate(results[:15], 1):
                path = result['metadata'].get('endpoint_path', 'unknown')
                method = result['metadata'].get('http_method', '?')
                score = result['bm25_score']
                
                # Highlight if it's a VM-related endpoint
                is_vm = " 🎯 VM ENDPOINT!" if 'vm' in path.lower() else ""
                
                print(f"   {i:2}. Score: {score:7.3f} | {method:6} {path}{is_vm}")
            
            # Step 5: Analysis and recommendations
            print(f"\n" + "=" * 80)
            print(f"5️⃣  ANALYSIS & RECOMMENDATIONS")
            print("=" * 80)
            
            # Check if VM endpoint exists
            has_vm_endpoint = any('vm' in ep.search_metadata.get('endpoint_path', '').lower() 
                                   for ep in all_endpoints if ep.search_metadata)
            
            # Check if VM endpoint is in top 10 results
            vm_in_top_10 = any('vm' in r['metadata'].get('endpoint_path', '').lower() 
                               for r in results[:10])
            
            if not has_vm_endpoint:
                print("\n❌ PROBLEM: No VM endpoint in knowledge base")
                print("\n💡 Possible causes:")
                print("   1. The vCenter OpenAPI spec doesn't contain /api/vcenter/vm")
                print("   2. The VM endpoint uses a different path")
                print("   3. Only a partial spec was uploaded")
                print("\n🔧 Solutions:")
                print("   1. Get the complete vCenter API spec (check VMware docs)")
                print("   2. Look for endpoint like /rest/vcenter/vm or /api/vcenter/virtual-machines")
                print("   3. Re-upload the correct OpenAPI spec")
                
            elif vm_in_top_10:
                print("\n✅ SUCCESS: VM endpoint IS being found by BM25!")
                print("   The search is working correctly.")
                
            else:
                print("\n⚠️  PROBLEM: VM endpoint exists but not in top 10 results")
                print("\n💡 This means the BM25 scoring needs tuning OR:")
                print("   1. The endpoint text doesn't match 'list virtual machines' well")
                print("   2. Other endpoints have better keyword matches")
                print("\n🔧 Let's check the VM endpoint text:")
                
                vm_ep = next((ep for ep in all_endpoints 
                             if ep.search_metadata and 'vm' in ep.search_metadata.get('endpoint_path', '').lower()), 
                            None)
                
                if vm_ep:
                    print(f"\n   Path: {vm_ep.search_metadata.get('endpoint_path')}")
                    print(f"   Text: {vm_ep.text[:300]}")
                    print("\n   👆 Does this text contain keywords like 'virtual machine', 'VM', 'list'?")
            
            print(f"\n" + "=" * 80)
            print("✅ Diagnostic complete!")
            print("=" * 80)
            
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

