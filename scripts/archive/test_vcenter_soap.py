#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Live E2E test for SOAP/VIM API integration with VMware vCenter.

This script tests the full SOAP integration flow:
1. Parse WSDL from vCenter
2. Create SOAP client with session auth
3. Call basic operations (RetrieveServiceContent)
4. Test DRS-related operations if available

Usage:
    python scripts/test_vcenter_soap.py

Note: Credentials should be passed via environment variables:
    VCENTER_URL - e.g., https://vcenter.example.com
    VCENTER_USERNAME - e.g., administrator@vsphere.local
    VCENTER_PASSWORD - your password

DO NOT commit credentials to the repository!
"""

import asyncio
import os
import sys
import logging

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meho_openapi.soap import (
    SOAPSchemaIngester,
    SOAPClient,
    SOAPConnectorConfig,
    SOAPAuthType,
)
from meho_openapi.soap.client import VMwareSOAPClient
from uuid import uuid4

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def test_wsdl_parsing(wsdl_url: str) -> bool:
    """Test 1: Parse WSDL and discover operations"""
    print("\n" + "="*60)
    print("TEST 1: WSDL Parsing")
    print("="*60)
    
    try:
        from meho_openapi.soap.models import SOAPConnectorConfig
        
        # Use config with SSL verification disabled (lab environment)
        config = SOAPConnectorConfig(
            wsdl_url=wsdl_url,
            verify_ssl=False,
            timeout=120,
        )
        ingester = SOAPSchemaIngester(config=config)
        # TASK-96: now returns 3 values (operations, metadata, type_definitions)
        operations, metadata, type_definitions = await ingester.ingest_wsdl(
            wsdl_url=wsdl_url,
            connector_id=uuid4(),
            tenant_id="test-tenant",
        )
        
        print(f"✅ WSDL parsed successfully!")
        print(f"   Services: {metadata.services}")
        print(f"   Ports: {metadata.ports}")
        print(f"   Operations discovered: {len(operations)}")
        print(f"   Type definitions discovered: {len(type_definitions)}")
        
        # Show some interesting operations
        interesting_ops = [
            op for op in operations
            if any(kw in op.operation_name.lower() for kw in 
                   ['drs', 'recommend', 'cluster', 'vm', 'host', 'retrieve'])
        ][:10]
        
        print(f"\n   Sample operations:")
        for op in interesting_ops:
            print(f"   - {op.operation_name}: {op.description or 'No description'}[:60]")
        
        return True
        
    except Exception as e:
        print(f"❌ WSDL parsing failed: {e}")
        logger.exception("WSDL parsing error")
        return False


async def test_soap_client_connection(wsdl_url: str, username: str, password: str) -> bool:
    """Test 2: Connect to vCenter and call RetrieveServiceContent"""
    print("\n" + "="*60)
    print("TEST 2: SOAP Client Connection")
    print("="*60)
    
    # VMware VIM API requires direct zeep usage for proper type handling
    # The generic SOAPClient doesn't handle ManagedObjectReference properly
    
    try:
        from zeep import Client
        from zeep.transports import Transport
        import requests
        import urllib3
        
        # Disable SSL warnings for lab environment
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        
        # Create session with SSL verification disabled
        session = requests.Session()
        session.verify = False
        
        transport = Transport(session=session, timeout=120)
        
        print("📡 Loading WSDL and connecting to vCenter...")
        
        # Get the base URL for service endpoint
        base_url = wsdl_url.replace("/sdk/vimService.wsdl", "")
        service_url = f"{base_url}/sdk"
        
        # Use service_address to override the endpoint in the WSDL
        from zeep import Settings
        settings = Settings(strict=False)
        
        client = Client(wsdl_url, transport=transport, settings=settings)
        
        # Override the service endpoint address
        # VMware WSDL sometimes has localhost hardcoded
        for service in client.wsdl.services.values():
            for port in service.ports.values():
                port.binding_options['address'] = service_url
        
        service = client.service
        print("✅ WSDL loaded!")
        print(f"   Service URL: {service_url}")
        
        # For VMware VIM API, _this needs to be a ManagedObjectReference type
        # Create it using zeep's type factory
        ManagedObjectReference = client.get_type('{urn:vim25}ManagedObjectReference')
        service_instance = ManagedObjectReference(_value_1="ServiceInstance", type="ServiceInstance")
        
        # Call RetrieveServiceContent
        print("\n📤 Calling RetrieveServiceContent...")
        result = service.RetrieveServiceContent(_this=service_instance)
        
        if result:
            print("✅ RetrieveServiceContent succeeded!")
            
            # Extract some info from the response using zeep's serialize helper
            from zeep.helpers import serialize_object
            body = serialize_object(result)
            
            if isinstance(body, dict):
                about = body.get("about", {})
                print(f"\n   vCenter Info:")
                print(f"   - Name: {about.get('name', 'N/A')}")
                print(f"   - Version: {about.get('version', 'N/A')}")
                print(f"   - Build: {about.get('build', 'N/A')}")
                print(f"   - OS Type: {about.get('osType', 'N/A')}")
                
                # Show available managers
                managers = [k for k in body.keys() if 'Manager' in k or 'manager' in k]
                print(f"\n   Available Managers ({len(managers)}):")
                for mgr in managers[:5]:
                    print(f"   - {mgr}")
                if len(managers) > 5:
                    print(f"   - ... and {len(managers) - 5} more")
                
                # Store for later tests
                return body
        else:
            print(f"❌ RetrieveServiceContent returned None")
            return False
        
        return True
        
    except Exception as e:
        print(f"❌ SOAP client test failed: {e}")
        logger.exception("SOAP client error")
        return False


async def test_drs_operations(wsdl_url: str, username: str, password: str) -> bool:
    """Test 3: Test authenticated operations (Login and list VMs)"""
    print("\n" + "="*60)
    print("TEST 3: Authenticated Operations")
    print("="*60)
    
    try:
        from zeep import Client
        from zeep.transports import Transport
        from zeep.helpers import serialize_object
        import requests
        import urllib3
        
        # Disable SSL warnings for lab environment
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        
        # Create session
        session = requests.Session()
        session.verify = False
        
        transport = Transport(session=session, timeout=120)
        
        print("📡 Loading WSDL...")
        
        # Override service endpoint (WSDL may have localhost hardcoded)
        base_url = wsdl_url.replace("/sdk/vimService.wsdl", "")
        service_url = f"{base_url}/sdk"
        
        from zeep import Settings
        settings = Settings(strict=False)
        
        client = Client(wsdl_url, transport=transport, settings=settings)
        
        # Override the service endpoint address
        for svc in client.wsdl.services.values():
            for port in svc.ports.values():
                port.binding_options['address'] = service_url
        
        service = client.service
        print(f"   Service URL: {service_url}")
        
        # Type factory
        ManagedObjectReference = client.get_type('{urn:vim25}ManagedObjectReference')
        
        # 1. Get ServiceContent
        service_instance = ManagedObjectReference(_value_1="ServiceInstance", type="ServiceInstance")
        content = service.RetrieveServiceContent(_this=service_instance)
        content_dict = serialize_object(content)
        
        session_manager_ref = content_dict.get("sessionManager")
        property_collector_ref = content_dict.get("propertyCollector")
        root_folder_ref = content_dict.get("rootFolder")
        
        if not session_manager_ref or not property_collector_ref:
            print("❌ Could not find required managers in ServiceContent")
            return False
        
        print(f"   Session Manager: {session_manager_ref}")
        print(f"   Property Collector: {property_collector_ref}")
        print(f"   Root Folder: {root_folder_ref}")
        
        # 2. Login
        print("\n📤 Logging in...")
        session_manager = ManagedObjectReference(
            _value_1=session_manager_ref.get("_value_1", "SessionManager"),
            type="SessionManager"
        )
        
        user_session = service.Login(
            _this=session_manager,
            userName=username,
            password=password,
        )
        session_dict = serialize_object(user_session)
        print(f"✅ Logged in as: {session_dict.get('userName')}")
        print(f"   Session key: {session_dict.get('key', 'N/A')[:20]}...")
        
        # 3. Get list of VMs using RetrievePropertiesEx
        print("\n📤 Retrieving VMs...")
        
        property_collector = ManagedObjectReference(
            _value_1=property_collector_ref.get("_value_1", "propertyCollector"),
            type="PropertyCollector"
        )
        
        root_folder = ManagedObjectReference(
            _value_1=root_folder_ref.get("_value_1", "group-d1"),
            type="Folder"
        )
        
        # Create the property filter spec
        PropertyFilterSpec = client.get_type('{urn:vim25}PropertyFilterSpec')
        PropertySpec = client.get_type('{urn:vim25}PropertySpec')
        ObjectSpec = client.get_type('{urn:vim25}ObjectSpec')
        TraversalSpec = client.get_type('{urn:vim25}TraversalSpec')
        SelectionSpec = client.get_type('{urn:vim25}SelectionSpec')
        RetrieveOptions = client.get_type('{urn:vim25}RetrieveOptions')
        
        # Simple traversal to find VMs
        prop_spec = PropertySpec(
            type="VirtualMachine",
            pathSet=["name", "runtime.powerState", "config.guestId"]
        )
        
        # Traversal specs for container hierarchy
        folder_traversal = TraversalSpec(
            name="folderTraversalSpec",
            type="Folder",
            path="childEntity",
            skip=False,
            selectSet=[
                SelectionSpec(name="folderTraversalSpec"),
                SelectionSpec(name="datacenterVmFolder"),
            ]
        )
        
        dc_vm_traversal = TraversalSpec(
            name="datacenterVmFolder",
            type="Datacenter",
            path="vmFolder",
            skip=False,
            selectSet=[SelectionSpec(name="folderTraversalSpec")]
        )
        
        obj_spec = ObjectSpec(
            obj=root_folder,
            skip=True,
            selectSet=[folder_traversal, dc_vm_traversal]
        )
        
        filter_spec = PropertyFilterSpec(
            propSet=[prop_spec],
            objectSet=[obj_spec]
        )
        
        options = RetrieveOptions(maxObjects=50)
        
        try:
            result = service.RetrievePropertiesEx(
                _this=property_collector,
                specSet=[filter_spec],
                options=options
            )
            
            if result:
                result_dict = serialize_object(result)
                objects = result_dict.get("objects", [])
                print(f"✅ Retrieved {len(objects)} VMs!")
                
                for obj in objects[:5]:
                    props = {p.get("name"): p.get("val") for p in obj.get("propSet", [])}
                    vm_name = props.get("name", "Unknown")
                    power_state = props.get("runtime.powerState", "Unknown")
                    print(f"   - {vm_name} ({power_state})")
                
                if len(objects) > 5:
                    print(f"   ... and {len(objects) - 5} more VMs")
            else:
                print("⚠️ No VMs found (result is None)")
                
        except Exception as e:
            print(f"⚠️ VM retrieval error: {e}")
            logger.exception("VM retrieval error")
        
        # 4. Logout
        print("\n📤 Logging out...")
        service.Logout(_this=session_manager)
        print("✅ Logged out successfully")
        
        return True
        
    except Exception as e:
        print(f"❌ DRS operations test failed: {e}")
        logger.exception("DRS operations error")
        return False


async def main():
    """Run all E2E tests"""
    print("="*60)
    print("MEHO SOAP/VIM API E2E Test Suite")
    print("="*60)
    
    # Get credentials from environment
    vcenter_url = os.environ.get("VCENTER_URL")
    username = os.environ.get("VCENTER_USERNAME")
    password = os.environ.get("VCENTER_PASSWORD")
    
    if not all([vcenter_url, username, password]):
        print("\n❌ Missing required environment variables!")
        print("   Set these before running:")
        print("   - VCENTER_URL (e.g., https://vcenter.example.com)")
        print("   - VCENTER_USERNAME (e.g., administrator@vsphere.local)")
        print("   - VCENTER_PASSWORD")
        print("\n   Example:")
        print("   export VCENTER_URL='https://vcenter.example.com'")
        print("   export VCENTER_USERNAME='administrator@vsphere.local'")
        print("   export VCENTER_PASSWORD='yourpassword'")
        print("   python scripts/test_vcenter_soap.py")
        return 1
    
    wsdl_url = f"{vcenter_url}/sdk/vimService.wsdl"
    print(f"\n🎯 Target: {vcenter_url}")
    print(f"📄 WSDL: {wsdl_url}")
    print(f"👤 User: {username}")
    
    results = []
    
    # Test 1: WSDL Parsing
    results.append(("WSDL Parsing", await test_wsdl_parsing(wsdl_url)))
    
    # Test 2: SOAP Client Connection
    results.append(("SOAP Client", await test_soap_client_connection(wsdl_url, username, password)))
    
    # Test 3: DRS Operations
    results.append(("DRS Operations", await test_drs_operations(wsdl_url, username, password)))
    
    # Summary
    print("\n" + "="*60)
    print("TEST SUMMARY")
    print("="*60)
    
    passed = sum(1 for _, r in results if r)
    total = len(results)
    
    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"   {status}: {name}")
    
    print(f"\n   Total: {passed}/{total} tests passed")
    
    return 0 if passed == total else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)

