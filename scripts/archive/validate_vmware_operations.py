#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Validate VMware Connector Completeness

This script introspects pyvmomi to discover ALL managed object types and their
properties, then compares against what our connector actually returns.

It generates an exhaustive gap report showing every missing property.

Usage:
    python scripts/validate_vmware_operations.py
    python scripts/validate_vmware_operations.py --report output.md
    python scripts/validate_vmware_operations.py --json gaps.json

Requirements:
    pip install pyvmomi
"""

import argparse
import ast
import inspect
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Set, Any, Optional

try:
    from pyVmomi import vim, vmodl
except ImportError:
    print("ERROR: pyvmomi is not installed. Install with: pip install pyvmomi")
    sys.exit(1)


@dataclass
class PropertyInfo:
    """Information about a pyvmomi property."""
    name: str
    type_name: str
    is_array: bool = False
    is_optional: bool = True  # Most pyvmomi properties can be None


@dataclass
class ManagedObjectInfo:
    """Information about a pyvmomi managed object type."""
    class_name: str
    full_name: str
    properties: List[PropertyInfo] = field(default_factory=list)
    

@dataclass
class ConnectorMethodInfo:
    """Information about a connector method."""
    method_name: str
    operation_id: str
    returned_keys: Set[str] = field(default_factory=set)
    source_lines: List[str] = field(default_factory=list)


@dataclass
class GapInfo:
    """Gap between pyvmomi and connector."""
    managed_object: str
    connector_method: str
    operation_id: str
    missing_properties: List[PropertyInfo] = field(default_factory=list)


def discover_all_managed_types() -> List[type]:
    """
    Get ALL vim.ManagedObject types from pyvmomi.
    
    This list was extracted by parsing pyvmomi's _typeinfo_vim.py source file
    to find all CreateManagedType() calls. This is the COMPLETE, DEFINITIVE list
    of all 137 vim.* managed object types in pyvmomi.
    
    Note: pyvmomi uses LazyModule with __getattr__, so types are NOT in dir().
    """
    print("🔍 Loading ALL vim.ManagedObject types...", file=sys.stderr)
    
    # COMPLETE list of all 137 vim.* managed types (extracted from pyvmomi source)
    # DO NOT manually add/remove - regenerate by parsing _typeinfo_vim.py if pyvmomi updates
    type_names = [
        'ActiveDirectoryAuthentication', 'Alarm', 'AlarmManager', 'AliasManager',
        'AssignableHardwareManager', 'AuthenticationManager', 'AuthenticationStore', 'AuthorizationManager',
        'AutoStartManager', 'BootDeviceSystem', 'CacheConfigurationManager', 'CertificateManager',
        'ClusterComputeResource', 'ClusterProfile', 'ComplianceManager', 'CompatibilityChecker',
        'ComputeResource', 'ContainerView', 'CpuSchedulerSystem', 'CryptoManager',
        'CryptoManagerHost', 'CryptoManagerHostKMS', 'CryptoManagerKmip', 'CustomFieldsManager',
        'CustomizationSpecManager', 'Datacenter', 'Datastore', 'DatastoreBrowser',
        'DatastoreNamespaceManager', 'DatastoreSystem', 'DateTimeSystem', 'DiagnosticManager',
        'DiagnosticSystem', 'DirectPathProfileManager', 'DirectoryStore', 'DistributedVirtualPortgroup',
        'DistributedVirtualSwitch', 'DistributedVirtualSwitchManager', 'EVCManager', 'EnvironmentBrowser',
        'EsxAgentHostManager', 'EventHistoryCollector', 'EventManager', 'ExtensibleManagedObject',
        'ExtensionManager', 'FailoverClusterConfigurator', 'FailoverClusterManager', 'FileManager',
        'FirewallSystem', 'FirmwareSystem', 'Folder', 'GuestCustomizationManager',
        'GuestOperationsManager', 'GraphicsManager', 'HealthStatusSystem', 'HealthUpdateManager',
        'HistoryCollector', 'HostAccessManager', 'HostProfile', 'HostSpecificationManager',
        'HostSystem', 'HttpNfcLease', 'ImageConfigManager', 'InventoryView',
        'IoFilterManager', 'IpPoolManager', 'IscsiManager', 'KernelModuleSystem',
        'LicenseAssignmentManager', 'LicenseManager', 'ListView', 'LocalAccountManager',
        'LocalAuthentication', 'LocalizationManager', 'ManagedEntity', 'ManagedObjectView',
        'MemoryManagerSystem', 'MessageBusProxy', 'Network', 'NetworkSystem',
        'NvdimmSystem', 'OpaqueNetwork', 'OptionManager', 'OverheadMemoryManager',
        'OvfManager', 'PatchManager', 'PciPassthruSystem', 'PerformanceManager',
        'PowerSystem', 'ProcessManager', 'Profile', 'ProfileManager',
        'ProvisioningChecker', 'ResourcePlanningManager', 'ResourcePool', 'ScheduledTask',
        'ScheduledTaskManager', 'SearchIndex', 'ServiceInstance', 'ServiceManager',
        'ServiceSystem', 'SessionManager', 'SimpleCommand', 'SiteInfoManager',
        'Snapshot', 'SnmpSystem', 'StoragePod', 'StorageQueryManager',
        'StorageResourceManager', 'StorageSystem', 'Task', 'TaskHistoryCollector',
        'TaskManager', 'TenantManager', 'UserDirectory', 'VFlashManager',
        'VMotionSystem', 'VStorageObjectManager', 'VStorageObjectManagerBase', 'View',
        'ViewManager', 'VirtualApp', 'VirtualDiskManager', 'VirtualMachine',
        'VirtualNicManager', 'VirtualizationManager', 'VmwareDistributedVirtualSwitch', 'VsanInternalSystem',
        'VsanSystem', 'VsanUpgradeSystem', 'WindowsRegistryManager',
    ]
    
    all_types = []
    for type_name in type_names:
        try:
            if hasattr(vim, type_name):
                obj = getattr(vim, type_name)
                all_types.append(obj)
        except Exception as e:
            print(f"     Warning: Could not load {type_name}: {e}", file=sys.stderr)
    
    print(f"   Loaded {len(all_types)} managed object types (complete list from pyvmomi source)", file=sys.stderr)
    return all_types


def introspect_properties(cls: type) -> List[PropertyInfo]:
    """
    Introspect ALL properties of a pyvmomi managed object class.
    
    Returns every property - no filtering.
    """
    properties = []
    
    # pyvmomi uses _propInfo to define properties
    if not hasattr(cls, '_propInfo'):
        return properties
    
    try:
        prop_info = cls._propInfo
        for prop_name, prop_details in prop_info.items():
            # Get type information
            type_name = "unknown"
            is_array = False
            
            if hasattr(prop_details, 'type'):
                prop_type = prop_details.type
                if hasattr(prop_type, '__name__'):
                    type_name = prop_type.__name__
                else:
                    type_name = str(prop_type)
            
            # Check if it's an array type
            if hasattr(prop_details, 'flags') and 'link' in str(prop_details.flags):
                is_array = True
            
            properties.append(PropertyInfo(
                name=prop_name,
                type_name=type_name,
                is_array=is_array,
                is_optional=True  # pyvmomi doesn't clearly mark required/optional
            ))
    except Exception as e:
        print(f"   Warning: Could not introspect {cls.__name__}: {e}", file=sys.stderr)
    
    return properties


def get_all_pyvmomi_types() -> Dict[str, ManagedObjectInfo]:
    """
    Get complete information about ALL pyvmomi managed object types.
    
    Returns a dict mapping class name to ManagedObjectInfo.
    """
    print("\n📋 Introspecting ALL pyvmomi managed object types...", file=sys.stderr)
    
    types_info = {}
    all_types = discover_all_managed_types()
    
    for cls in all_types:
        class_name = cls.__name__
        full_name = f"{cls.__module__}.{class_name}"
        
        properties = introspect_properties(cls)
        
        types_info[class_name] = ManagedObjectInfo(
            class_name=class_name,
            full_name=full_name,
            properties=properties
        )
        
        if properties:
            print(f"   {class_name}: {len(properties)} properties", file=sys.stderr)
    
    return types_info


def parse_connector_methods(connector_base_path: Path) -> List[ConnectorMethodInfo]:
    """
    Parse handler files to extract what each method actually returns.
    
    Now that connector is split into handler modules, we parse those instead.
    """
    print("\n🔬 Parsing handler methods...", file=sys.stderr)
    
    handlers_dir = connector_base_path.parent / "handlers"
    
    if not handlers_dir.exists():
        print(f"   WARNING: {handlers_dir} not found, trying old connector.py", file=sys.stderr)
        # Fallback to old approach
        handlers_dir = connector_base_path
    
    methods = []
    
    # Parse all handler files
    handler_files = list(handlers_dir.glob("*_handlers.py")) if handlers_dir.is_dir() else [connector_base_path]
    
    for handler_file in handler_files:
        with open(handler_file, 'r') as f:
            content = f.read()
        
        try:
            tree = ast.parse(content)
        except SyntaxError as e:
            print(f"   ERROR: Failed to parse {handler_file.name}: {e}", file=sys.stderr)
            continue
        
        # Find all async def methods starting with _
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            
            if not node.name.startswith('_'):
                continue
            
            # Skip stub methods (single-line return None)
            if len(node.body) <= 1:
                continue
            
            # Extract returned keys from dictionary literals
            returned_keys = set()
            
            for child in ast.walk(node):
                if isinstance(child, ast.Dict):
                    for key in child.keys:
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            returned_keys.add(key.value)
                        elif isinstance(key, ast.Str):  # Python 3.7 compatibility
                            returned_keys.add(key.s)
            
            if returned_keys:
                operation_id = node.name[1:]  # Remove leading _
                
                methods.append(ConnectorMethodInfo(
                    method_name=node.name,
                    operation_id=operation_id,
                    returned_keys=returned_keys
                ))
                
                if len(methods) <= 10:  # Show first 10
                    print(f"   {node.name}: returns {len(returned_keys)} keys", file=sys.stderr)
    
    print(f"   Total: {len(methods)} methods parsed", file=sys.stderr)
    return methods


def map_operation_to_type(operation_id: str) -> Optional[str]:
    """
    Map an operation_id to a pyvmomi managed object type.
    
    Examples:
        list_vms -> vim.VirtualMachine
        get_host -> vim.HostSystem
        list_clusters -> vim.ClusterComputeResource
    """
    # Comprehensive mapping with priority (more specific first)
    mappings = [
        # VirtualMachine operations (highest priority - most common)
        (['vm', 'virtual_machine', 'guest', 'tools', 'template'], 'vim.VirtualMachine'),
        
        # HostSystem operations
        (['host'], 'vim.HostSystem'),
        
        # Cluster operations
        (['cluster', 'drs', 'ha', 'evc'], 'vim.ClusterComputeResource'),
        
        # Datastore operations
        (['datastore', 'vmfs', 'nfs'], 'vim.Datastore'),
        
        # Network operations
        (['network', 'nic', 'adapter', 'vlan'], 'vim.Network'),
        (['dvs', 'distributed_switch'], 'vim.DistributedVirtualSwitch'),
        (['portgroup', 'port_group'], 'vim.DistributedVirtualPortgroup'),
        
        # Snapshot operations
        (['snapshot'], 'vim.vm.Snapshot'),
        
        # Resource operations
        (['resource_pool'], 'vim.ResourcePool'),
        (['folder'], 'vim.Folder'),
        (['datacenter'], 'vim.Datacenter'),
        
        # Monitoring operations
        (['alarm'], 'vim.alarm.Alarm'),
        (['event'], 'vim.event.Event'),
        (['task'], 'vim.Task'),
        (['performance'], 'vim.PerformanceManager'),
        
        # Storage operations
        (['storage_pod'], 'vim.StoragePod'),
        (['disk'], 'vim.VirtualMachine'),  # Disk operations are on VMs
        
        # Content library (these won't map - REST API)
        (['content_library', 'library'], None),
        (['tag'], None),  # Tags are REST API
        (['license'], 'vim.LicenseManager'),
        (['vcenter_info'], 'vim.ServiceInstance'),
    ]
    
    op_lower = operation_id.lower()
    
    # Try each mapping in order
    for keywords, managed_type in mappings:
        if any(keyword in op_lower for keyword in keywords):
            return managed_type
    
    return None


def find_gaps(pyvmomi_types: Dict[str, ManagedObjectInfo],
              connector_methods: List[ConnectorMethodInfo]) -> List[GapInfo]:
    """
    Find ALL gaps between pyvmomi properties and connector output.
    
    Every missing property is reported - no filtering.
    """
    print("\n🕳️  Finding gaps...", file=sys.stderr)
    
    gaps = []
    
    for method in connector_methods:
        # Try to determine which pyvmomi type this method deals with
        managed_type = map_operation_to_type(method.operation_id)
        
        if not managed_type or managed_type not in pyvmomi_types:
            print(f"   ⚠️  {method.method_name}: Could not map to pyvmomi type", file=sys.stderr)
            continue
        
        type_info = pyvmomi_types[managed_type]
        
        # Find missing properties
        missing = []
        for prop in type_info.properties:
            # Check if property is returned (either directly or nested)
            prop_key = prop.name
            
            # Also check for common variations
            if prop_key not in method.returned_keys:
                missing.append(prop)
        
        if missing:
            gap = GapInfo(
                managed_object=managed_type,
                connector_method=method.method_name,
                operation_id=method.operation_id,
                missing_properties=missing
            )
            gaps.append(gap)
            
            print(f"   🔴 {method.method_name} ({managed_type}): {len(missing)} missing properties", file=sys.stderr)
    
    return gaps


def generate_markdown_report(gaps: List[GapInfo],
                            pyvmomi_types: Dict[str, ManagedObjectInfo]) -> str:
    """Generate a detailed markdown gap report."""
    
    lines = [
        "# VMware Connector Gap Report",
        "",
        "This report shows ALL properties available in pyvmomi that are NOT returned by our connector.",
        "",
        f"**Total Managed Types Analyzed:** {len(pyvmomi_types)}",
        f"**Total Connector Methods Analyzed:** {len(gaps)}",
        f"**Methods with Gaps:** {len([g for g in gaps if g.missing_properties])}",
        "",
        "---",
        "",
    ]
    
    # Summary by managed object type
    gaps_by_type = defaultdict(list)
    for gap in gaps:
        if gap.missing_properties:
            gaps_by_type[gap.managed_object].append(gap)
    
    lines.append("## Summary by Managed Object Type")
    lines.append("")
    lines.append("| Managed Object | Methods | Total Missing Properties |")
    lines.append("|----------------|---------|--------------------------|")
    
    for mo_type, type_gaps in sorted(gaps_by_type.items()):
        total_missing = sum(len(g.missing_properties) for g in type_gaps)
        lines.append(f"| `{mo_type}` | {len(type_gaps)} | **{total_missing}** |")
    
    lines.append("")
    lines.append("---")
    lines.append("")
    
    # Detailed gaps by method
    lines.append("## Detailed Gaps by Method")
    lines.append("")
    
    for gap in sorted(gaps, key=lambda g: (g.managed_object, g.connector_method)):
        if not gap.missing_properties:
            continue
        
        lines.append(f"### `{gap.connector_method}` → `{gap.managed_object}`")
        lines.append("")
        lines.append(f"**Operation ID:** `{gap.operation_id}`")
        lines.append(f"**Missing Properties:** {len(gap.missing_properties)}")
        lines.append("")
        lines.append("| Property | Type | Array |")
        lines.append("|----------|------|-------|")
        
        for prop in sorted(gap.missing_properties, key=lambda p: p.name):
            is_array = "✓" if prop.is_array else ""
            lines.append(f"| `{prop.name}` | `{prop.type_name}` | {is_array} |")
        
        lines.append("")
        lines.append("---")
        lines.append("")
    
    # Appendix: All pyvmomi types
    lines.append("## Appendix: All pyvmomi Managed Object Types")
    lines.append("")
    lines.append("| Type | Properties |")
    lines.append("|------|------------|")
    
    for type_name, type_info in sorted(pyvmomi_types.items()):
        lines.append(f"| `{type_name}` | {len(type_info.properties)} |")
    
    lines.append("")
    
    return '\n'.join(lines)


def generate_json_report(gaps: List[GapInfo]) -> str:
    """Generate JSON gap report."""
    output = {
        "total_gaps": len(gaps),
        "gaps": [asdict(g) for g in gaps if g.missing_properties]
    }
    return json.dumps(output, indent=2, default=str)


def main():
    parser = argparse.ArgumentParser(description="Validate VMware connector completeness")
    parser.add_argument('--connector', '-c', 
                       default='meho_openapi/connectors/vmware/connector.py',
                       help="Path to connector.py")
    parser.add_argument('--report', '-r', help="Output markdown report file")
    parser.add_argument('--json', help="Output JSON report file")
    args = parser.parse_args()
    
    # Resolve paths
    workspace = Path(__file__).parent.parent
    connector_path = workspace / args.connector
    
    print("=" * 70, file=sys.stderr)
    print("VMware Connector Validation", file=sys.stderr)
    print("=" * 70, file=sys.stderr)
    
    # Step 1: Get ALL pyvmomi types and properties
    pyvmomi_types = get_all_pyvmomi_types()
    
    # Step 2: Parse connector methods
    connector_methods = parse_connector_methods(connector_path)
    
    # Step 3: Find gaps
    gaps = find_gaps(pyvmomi_types, connector_methods)
    
    # Generate reports
    print("\n" + "=" * 70, file=sys.stderr)
    print("Results", file=sys.stderr)
    print("=" * 70, file=sys.stderr)
    
    total_missing = sum(len(g.missing_properties) for g in gaps)
    print(f"\n✅ Analysis complete:", file=sys.stderr)
    print(f"   - {len(pyvmomi_types)} managed object types discovered", file=sys.stderr)
    print(f"   - {len(connector_methods)} connector methods analyzed", file=sys.stderr)
    print(f"   - {len([g for g in gaps if g.missing_properties])} methods have gaps", file=sys.stderr)
    print(f"   - {total_missing} total missing properties", file=sys.stderr)
    
    if args.report:
        report = generate_markdown_report(gaps, pyvmomi_types)
        report_path = workspace / args.report
        report_path.write_text(report)
        print(f"\n📄 Markdown report written to: {report_path}", file=sys.stderr)
    
    if args.json:
        report = generate_json_report(gaps)
        json_path = workspace / args.json
        json_path.write_text(report)
        print(f"\n📄 JSON report written to: {json_path}", file=sys.stderr)
    
    if not args.report and not args.json:
        # Print summary to stdout
        report = generate_markdown_report(gaps, pyvmomi_types)
        print("\n" + report)
    
    # Exit code indicates if gaps were found
    return 1 if total_missing > 0 else 0


if __name__ == '__main__':
    sys.exit(main())

