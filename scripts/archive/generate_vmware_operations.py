#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Generate VMware Operation Definitions from pyvmomi Introspection

This script introspects the pyvmomi library to discover all available
operations and their exact signatures, then generates OperationDefinition
objects for use in the MEHO VMware connector.

Usage:
    python scripts/generate_vmware_operations.py
    python scripts/generate_vmware_operations.py --output meho_openapi/connectors/vmware/operations_generated.py
    python scripts/generate_vmware_operations.py --json  # Output as JSON for inspection

Requirements:
    pip install pyvmomi
"""

import argparse
import json
import sys
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, asdict

try:
    from pyVmomi import vim
except ImportError:
    print("ERROR: pyvmomi is not installed. Install with: pip install pyvmomi")
    sys.exit(1)


@dataclass
class OperationParam:
    """Parameter definition for an operation."""
    name: str
    type: str
    required: bool
    description: str


@dataclass
class OperationDef:
    """Definition of a VMware operation."""
    operation_id: str
    name: str
    description: str
    category: str
    managed_object: str
    pyvmomi_method: str
    parameters: List[OperationParam]
    returns: str
    is_task: bool  # Returns vim.Task (async operation)
    
    def to_python_code(self) -> str:
        """Generate Python code for OperationDefinition."""
        params_code = "[]"
        if self.parameters:
            params_list = []
            for p in self.parameters:
                params_list.append(
                    f'{{"name": "{p.name}", "type": "{p.type}", '
                    f'"required": {str(p.required)}, "description": "{p.description}"}}'
                )
            params_code = "[\n            " + ",\n            ".join(params_list) + "\n        ]"
        
        example_params = ", ".join([f'{p.name}=...' for p in self.parameters if p.required])
        example = f"{self.operation_id}({example_params})"
        
        return f'''    OperationDefinition(
        operation_id="{self.operation_id}",
        name="{self.name}",
        description="{self.description}",
        category="{self.category}",
        parameters={params_code},
        example="{example}",
    ),'''


def get_pyvmomi_type_name(pyvmomi_type) -> str:
    """Convert pyvmomi type to human-readable string."""
    if hasattr(pyvmomi_type, '__name__'):
        name = pyvmomi_type.__name__
        # Simplify common types
        if name == 'str':
            return 'string'
        elif name == 'int':
            return 'integer'
        elif name == 'long':
            return 'integer'
        elif name == 'bool':
            return 'boolean'
        elif name.startswith('vim.'):
            return name
        return name
    return str(pyvmomi_type)


def introspect_managed_object(cls, category: str) -> List[OperationDef]:
    """Introspect a pyvmomi managed object class and extract operations."""
    operations = []
    class_name = cls.__name__
    
    for method_name in dir(cls):
        if method_name.startswith('_'):
            continue
        
        try:
            method = getattr(cls, method_name)
            
            # Check if it's a callable method with info
            if not hasattr(method, 'info') or not hasattr(method.info, 'params'):
                continue
            
            info = method.info
            
            # Skip duplicates (methods ending in _Task have non-Task versions)
            if not method_name.endswith('_Task') and hasattr(cls, f'{method_name}_Task'):
                continue  # Skip the non-Task version, prefer _Task
            
            # Extract parameters
            params = []
            for param in info.params:
                param_type = get_pyvmomi_type_name(param.type)
                # Determine if optional (pyvmomi doesn't expose this well)
                required = True  # Default to required
                
                params.append(OperationParam(
                    name=param.name,
                    type=param_type,
                    required=required,
                    description=f"{param.name} parameter"
                ))
            
            # Get return type
            returns = get_pyvmomi_type_name(info.result) if info.result else "None"
            is_task = returns == "vim.Task"
            
            # Generate operation ID (snake_case)
            op_id = method_name
            if op_id.endswith('_Task'):
                op_id = op_id[:-5]  # Remove _Task suffix
            # Convert CamelCase to snake_case
            op_id = ''.join(['_' + c.lower() if c.isupper() else c for c in op_id]).lstrip('_')
            
            # Generate human-readable name
            name = method_name.replace('_Task', '').replace('_', ' ')
            # Add spaces before capitals
            name = ''.join([' ' + c if c.isupper() and i > 0 else c for i, c in enumerate(name)])
            name = name.strip()
            
            operations.append(OperationDef(
                operation_id=op_id,
                name=name,
                description=f"{name} on {class_name}",
                category=category,
                managed_object=class_name,
                pyvmomi_method=method_name,
                parameters=params,
                returns=returns,
                is_task=is_task
            ))
            
        except Exception as e:
            # Skip methods that can't be introspected
            pass
    
    return operations


def generate_all_operations() -> Dict[str, List[OperationDef]]:
    """Generate operations for all major pyvmomi managed objects."""
    
    managed_objects = {
        "VirtualMachine": (vim.VirtualMachine, "compute"),
        "HostSystem": (vim.HostSystem, "compute"),
        "ClusterComputeResource": (vim.ClusterComputeResource, "compute"),
        "Datastore": (vim.Datastore, "storage"),
        "Network": (vim.Network, "networking"),
        "DistributedVirtualSwitch": (vim.DistributedVirtualSwitch, "networking"),
        "Datacenter": (vim.Datacenter, "inventory"),
        "Folder": (vim.Folder, "inventory"),
        "ResourcePool": (vim.ResourcePool, "compute"),
        "VmSnapshot": (vim.vm.Snapshot, "compute"),
    }
    
    all_operations = {}
    
    for name, (cls, category) in managed_objects.items():
        operations = introspect_managed_object(cls, category)
        all_operations[name] = operations
        print(f"Found {len(operations)} operations for {name}", file=sys.stderr)
    
    return all_operations


def generate_python_file(operations: Dict[str, List[OperationDef]]) -> str:
    """Generate Python file content."""
    
    lines = [
        '"""',
        'VMware Operation Definitions - AUTO-GENERATED from pyvmomi introspection',
        '',
        'Generated by: scripts/generate_vmware_operations.py',
        '',
        'DO NOT EDIT MANUALLY - Regenerate with:',
        '    python scripts/generate_vmware_operations.py --output meho_openapi/connectors/vmware/operations.py',
        '"""',
        '',
        'from meho_openapi.connectors.base import OperationDefinition',
        '',
        '',
    ]
    
    for mo_name, ops in operations.items():
        if not ops:
            continue
            
        lines.append(f'# {"=" * 70}')
        lines.append(f'# {mo_name} Operations ({len(ops)} operations)')
        lines.append(f'# {"=" * 70}')
        lines.append('')
        lines.append(f'{mo_name.upper()}_OPERATIONS = [')
        
        for op in ops:
            lines.append(op.to_python_code())
        
        lines.append(']')
        lines.append('')
        lines.append('')
    
    # Generate combined list
    lines.append('# Combined list of all operations')
    lines.append('VMWARE_OPERATIONS = (')
    for mo_name in operations.keys():
        if operations[mo_name]:
            lines.append(f'    {mo_name.upper()}_OPERATIONS +')
    lines.append('    []  # Empty list to handle trailing +')
    lines.append(')')
    lines.append('')
    
    return '\n'.join(lines)


def generate_json(operations: Dict[str, List[OperationDef]]) -> str:
    """Generate JSON output for inspection."""
    output = {}
    for mo_name, ops in operations.items():
        output[mo_name] = [asdict(op) for op in ops]
    return json.dumps(output, indent=2)


def generate_markdown_table(operations: Dict[str, List[OperationDef]]) -> str:
    """Generate markdown table of all operations."""
    lines = [
        "# VMware Operations Reference (from pyvmomi introspection)",
        "",
    ]
    
    for mo_name, ops in operations.items():
        if not ops:
            continue
        
        lines.append(f"## {mo_name}")
        lines.append("")
        lines.append("| Operation | Parameters | Returns | Async |")
        lines.append("|-----------|------------|---------|-------|")
        
        for op in ops:
            params = ", ".join([f"`{p.name}`" for p in op.parameters])
            is_async = "✓" if op.is_task else ""
            lines.append(f"| `{op.pyvmomi_method}` | {params} | `{op.returns}` | {is_async} |")
        
        lines.append("")
    
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description="Generate VMware operations from pyvmomi")
    parser.add_argument('--output', '-o', help="Output file path")
    parser.add_argument('--json', action='store_true', help="Output as JSON")
    parser.add_argument('--markdown', action='store_true', help="Output as Markdown table")
    args = parser.parse_args()
    
    print("Introspecting pyvmomi managed objects...", file=sys.stderr)
    operations = generate_all_operations()
    
    total = sum(len(ops) for ops in operations.values())
    print(f"\nTotal operations discovered: {total}", file=sys.stderr)
    
    if args.json:
        output = generate_json(operations)
    elif args.markdown:
        output = generate_markdown_table(operations)
    else:
        output = generate_python_file(operations)
    
    if args.output:
        with open(args.output, 'w') as f:
            f.write(output)
        print(f"\nWritten to: {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == '__main__':
    main()

