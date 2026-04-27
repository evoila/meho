#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Split vmware/connector.py into organized handler modules.

Uses mixin pattern to organize the large connector class into manageable pieces.
"""

import re
from pathlib import Path
from typing import List, Tuple


def extract_method(lines: List[str], start_idx: int) -> Tuple[str, int]:
    """
    Extract a complete method starting at start_idx.
    Returns (method_text, end_idx).
    """
    method_lines = [lines[start_idx]]
    indent_level = len(lines[start_idx]) - len(lines[start_idx].lstrip())
    
    i = start_idx + 1
    while i < len(lines):
        line = lines[i]
        
        # Empty lines are always included
        if not line.strip():
            method_lines.append(line)
            i += 1
            continue
        
        # Check indentation
        current_indent = len(line) - len(line.lstrip())
        
        # If we hit a line at the same or lower indentation and it's a def/class/comment block, we're done
        if current_indent <= indent_level:
            if line.strip().startswith(('async def', 'def ', 'class ', '#')):
                break
        
        method_lines.append(line)
        i += 1
    
    return '\n'.join(method_lines), i


def categorize_method(method_name: str) -> str:
    """Determine which category a method belongs to."""
    
    # VM-related (highest priority - most specific patterns)
    vm_patterns = [
        'vm', 'guest', 'snapshot', 'template', 'disk', 'nic', 'screen',
        'custom', 'clone', 'migrate', 'relocate', 'tools', 'ovf', 'nmi',
        'fault_tolerance', 'boot_options', 'annotation', 'question',
        'mks', 'ticket', 'changed_disk', 'instant',
    ]
    
    # Host-related
    host_patterns = [
        'host', 'maintenance', 'lockdown', 'standby', 'firewall',
        'service', 'cim', 'tpm', 'hardware_uptime', 'memory_overhead',
        'connection_info', 'flags', 'storage' if '_scan_host_storage' in method_name or '_refresh_storage_info' in method_name else None,
    ]
    
    # Cluster-related
    cluster_patterns = ['cluster', 'drs', '_ha_', 'evc', 'place_vm', 'rules_for_vm', 'hosts_for_vm']
    
    # Storage-related
    storage_patterns = ['datastore', 'vmfs', 'nfs', 'storage_pod', 'browse']
    
    # Network-related
    network_patterns = ['network', 'dvs', 'distributed_switch', 'port_group', 'portgroup', 'vlan', 'adapter']
    
    # Inventory-related
    inventory_patterns = ['folder', 'datacenter', 'resource_pool', 'tag', 'library', 'inventory', 'search']
    
    # System-related
    system_patterns = ['vcenter', 'task', 'alarm', 'event', 'license', 'performance']
    
    method_lower = method_name.lower()
    
    # Check patterns in order of specificity
    for pattern in vm_patterns:
        if pattern and pattern in method_lower:
            return 'vm'
    
    for pattern in host_patterns:
        if pattern and pattern in method_lower:
            return 'host'
    
    for pattern in cluster_patterns:
        if pattern and pattern in method_lower:
            return 'cluster'
    
    for pattern in storage_patterns:
        if pattern and pattern in method_lower:
            return 'storage'
    
    for pattern in network_patterns:
        if pattern and pattern in method_lower:
            return 'network'
    
    for pattern in inventory_patterns:
        if pattern and pattern in method_lower:
            return 'inventory'
    
    for pattern in system_patterns:
        if pattern and pattern in method_lower:
            return 'system'
    
    return 'uncategorized'


def split_connector():
    """Split connector.py into handler modules."""
    
    connector_path = Path("meho_openapi/connectors/vmware/connector.py")
    content = connector_path.read_text()
    lines = content.split('\n')
    
    # Find all async def methods (class methods only)
    methods_by_category = {
        'vm': [],
        'host': [],
        'cluster': [],
        'storage': [],
        'network': [],
        'inventory': [],
        'system': [],
        'uncategorized': [],
    }
    
    i = 0
    while i < len(lines):
        line = lines[i]
        
        # Look for async def methods
        match = re.match(r'^    async def (_[a-z_]+)\(self, params:', line)
        if match:
            method_name = match.group(1)
            
            # Skip methods we've already extracted
            if method_name.startswith('_serialize_') or method_name.startswith('_find_') or \
               method_name in ('_collect_snapshots', '_search_snapshot_tree', '_make_guest_auth'):
                i += 1
                continue
            
            # Extract the complete method
            method_text, end_idx = extract_method(lines, i)
            category = categorize_method(method_name)
            
            methods_by_category[category].append((method_name, method_text))
            print(f"  {method_name} → {category}")
            
            i = end_idx
        else:
            i += 1
    
    # Print summary
    print(f"\n📊 Extraction Summary:")
    total = 0
    for category, methods in methods_by_category.items():
        if methods:
            print(f"  {category}: {len(methods)} methods")
            total += len(methods)
    print(f"  TOTAL: {total} methods")
    
    return methods_by_category


def write_handler_files(methods_by_category):
    """Write handler mixin files."""
    
    base_dir = Path("meho_openapi/connectors/vmware/handlers")
    base_dir.mkdir(exist_ok=True)
    
    header_template = '''"""
{title}

Mixin class containing {count} {category} operation handlers.
"""

from typing import List, Dict, Any


class {class_name}:
    """Mixin for {category} operation handlers."""
    
    # These will be provided by VMwareConnector
    _content: Any
    
'''
    
    category_info = {
        'vm': ('VM Operation Handlers', 'VMHandlerMixin'),
        'host': ('Host Operation Handlers', 'HostHandlerMixin'),
        'cluster': ('Cluster Operation Handlers', 'ClusterHandlerMixin'),
        'storage': ('Storage Operation Handlers', 'StorageHandlerMixin'),
        'network': ('Network Operation Handlers', 'NetworkHandlerMixin'),
        'inventory': ('Inventory Operation Handlers', 'InventoryHandlerMixin'),
        'system': ('System Operation Handlers', 'SystemHandlerMixin'),
    }
    
    for category, methods in methods_by_category.items():
        if category == 'uncategorized' or not methods:
            continue
        
        title, class_name = category_info[category]
        filepath = base_dir / f"{category}_handlers.py"
        
        file_content = header_template.format(
            title=title,
            count=len(methods),
            category=category,
            class_name=class_name
        )
        
        # Add all methods
        for method_name, method_text in methods:
            file_content += method_text + '\n\n'
        
        filepath.write_text(file_content)
        print(f"✅ Created {filepath.name} with {len(methods)} methods")
    
    # Create __init__.py
    init_content = '''"""
VMware Connector Handler Mixins
"""

from .vm_handlers import VMHandlerMixin
from .host_handlers import HostHandlerMixin
from .cluster_handlers import ClusterHandlerMixin
from .storage_handlers import StorageHandlerMixin
from .network_handlers import NetworkHandlerMixin
from .inventory_handlers import InventoryHandlerMixin
from .system_handlers import SystemHandlerMixin

__all__ = [
    'VMHandlerMixin',
    'HostHandlerMixin',
    'ClusterHandlerMixin',
    'StorageHandlerMixin',
    'NetworkHandlerMixin',
    'InventoryHandlerMixin',
    'SystemHandlerMixin',
]
'''
    (base_dir / '__init__.py').write_text(init_content)
    print(f"✅ Created handlers/__init__.py")


if __name__ == '__main__':
    print("🔍 Extracting methods from connector.py...\n")
    methods = split_connector()
    print("\n📝 Writing handler files...\n")
    write_handler_files(methods)
    print("\n✅ Split complete!")

SPLIT_SCRIPT

