#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Audit Operation Descriptions vs Handler Implementations

Systematically compares what each operation PROMISES in its description
against what the handler actually RETURNS.

Usage:
    python scripts/audit_operation_descriptions.py
    python scripts/audit_operation_descriptions.py --report operation_audit.md
"""

import argparse
import ast
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Set


@dataclass
class OperationInfo:
    """Information about an operation definition."""
    operation_id: str
    name: str
    description: str
    category: str
    parameters: List[str] = field(default_factory=list)


@dataclass
class HandlerInfo:
    """Information about a handler implementation."""
    method_name: str
    returned_keys: Set[str] = field(default_factory=set)
    returns_type: str = "dict"  # dict, list, str, etc.


@dataclass
class AuditResult:
    """Audit result for an operation."""
    operation_id: str
    category: str
    description: str
    promised_fields: Set[str] = field(default_factory=set)
    actual_fields: Set[str] = field(default_factory=set)
    missing_fields: Set[str] = field(default_factory=set)
    status: str = "unknown"  # match, incomplete, mismatch, no_handler


def parse_operations() -> Dict[str, OperationInfo]:
    """Parse all operation definitions from category files."""
    print("📖 Parsing operation definitions...", file=sys.stderr)
    
    operations = {}
    ops_dir = Path("meho_openapi/connectors/vmware/operations")
    
    for ops_file in ops_dir.glob("*.py"):
        if ops_file.name == "__init__.py":
            continue
        
        with open(ops_file, 'r') as f:
            content = f.read()
        
        # Extract OperationDefinition blocks
        pattern = r'OperationDefinition\(\s*operation_id="([^"]+)",\s*name="([^"]+)",\s*description="([^"]+)",\s*category="([^"]+)"'
        matches = re.findall(pattern, content, re.DOTALL)
        
        for op_id, name, description, category in matches:
            operations[op_id] = OperationInfo(
                operation_id=op_id,
                name=name,
                description=description,
                category=category
            )
    
    print(f"   Found {len(operations)} operations", file=sys.stderr)
    return operations


def parse_handlers() -> Dict[str, HandlerInfo]:
    """Parse all handler implementations to extract what they return."""
    print("🔍 Parsing handler implementations...", file=sys.stderr)
    
    handlers = {}
    handlers_dir = Path("meho_openapi/connectors/vmware/handlers")
    
    for handler_file in handlers_dir.glob("*_handlers.py"):
        with open(handler_file, 'r') as f:
            content = f.read()
        
        try:
            tree = ast.parse(content)
        except SyntaxError:
            continue
        
        # Find all async def methods
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            
            if not node.name.startswith('_'):
                continue
            
            # Extract returned keys
            returned_keys = set()
            returns_list = False
            
            for child in ast.walk(node):
                # Check if returns a list
                if isinstance(child, ast.Return) and isinstance(child.value, ast.ListComp):
                    returns_list = True
                
                # Extract dict keys
                if isinstance(child, ast.Dict):
                    for key in child.keys:
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            returned_keys.add(key.value)
            
            if returned_keys or returns_list:
                handlers[node.name] = HandlerInfo(
                    method_name=node.name,
                    returned_keys=returned_keys,
                    returns_type="list" if returns_list else "dict"
                )
    
    print(f"   Found {len(handlers)} handlers", file=sys.stderr)
    return handlers


def extract_promised_fields(description: str) -> Set[str]:
    """
    Extract fields promised in description.
    
    Looks for patterns like:
    - "Returns X, Y, and Z"
    - "Get X and Y"
    - "includes X, Y, Z"
    """
    promised = set()
    desc_lower = description.lower()
    
    # Common field keywords to look for
    field_keywords = [
        'cpu', 'memory', 'disk', 'network', 'storage', 'host', 'cluster',
        'datastore', 'ip', 'power', 'state', 'status', 'config', 'hardware',
        'guest', 'tools', 'snapshot', 'alarm', 'event', 'task', 'performance',
        'capacity', 'usage', 'iops', 'latency', 'throughput', 'drs', 'ha',
        'version', 'uuid', 'name', 'boot', 'uptime',
    ]
    
    for keyword in field_keywords:
        if keyword in desc_lower:
            promised.add(keyword)
    
    return promised


def audit_operations(operations: Dict[str, OperationInfo],
                     handlers: Dict[str, HandlerInfo]) -> List[AuditResult]:
    """Audit all operations against their handlers."""
    print("🔬 Auditing operations vs handlers...", file=sys.stderr)
    
    # Build operation_id to method_name mapping from connector.py
    connector_file = Path("meho_openapi/connectors/vmware/connector.py")
    connector_content = connector_file.read_text()
    
    # Extract handler mappings from execute() method
    op_to_method = {}
    mapping_pattern = r'"([^"]+)":\s*self\.(_[a-z_]+)'
    for op_id, method_name in re.findall(mapping_pattern, connector_content):
        op_to_method[op_id] = method_name
    
    print(f"   Found {len(op_to_method)} operation→method mappings", file=sys.stderr)
    
    results = []
    
    for op_id, op_info in operations.items():
        # Use the mapping from connector.py
        method_name = op_to_method.get(op_id, f"_{op_id}")
        
        # Check if handler exists
        if method_name not in handlers:
            result = AuditResult(
                operation_id=op_id,
                category=op_info.category,
                description=op_info.description[:100],
                status="no_handler"
            )
            results.append(result)
            continue
        
        handler_info = handlers[method_name]
        
        # Extract what description promises
        promised = extract_promised_fields(op_info.description)
        actual = handler_info.returned_keys
        
        # Find gaps
        # Note: This is heuristic - not all promised fields need to be top-level keys
        missing = set()
        for field in promised:
            field_found = any(field in str(actual).lower() for k in actual)
            if not field_found:
                missing.add(field)
        
        # Determine status
        if not promised:
            status = "vague"  # Description doesn't specify fields
        elif not missing:
            status = "match"
        elif len(missing) < len(promised) / 2:
            status = "incomplete"
        else:
            status = "mismatch"
        
        result = AuditResult(
            operation_id=op_id,
            category=op_info.category,
            description=op_info.description,
            promised_fields=promised,
            actual_fields=actual,
            missing_fields=missing,
            status=status
        )
        results.append(result)
    
    return results


def generate_markdown_report(results: List[AuditResult]) -> str:
    """Generate comprehensive markdown audit report."""
    lines = [
        "# VMware Operation Description Audit Report",
        "",
        "Systematic comparison of operation descriptions vs handler implementations.",
        "",
        f"**Total Operations**: {len(results)}",
        "",
    ]
    
    # Summary by status
    by_status = defaultdict(list)
    for r in results:
        by_status[r.status].append(r)
    
    lines.append("## Summary")
    lines.append("")
    lines.append("| Status | Count | Description |")
    lines.append("|--------|-------|-------------|")
    lines.append(f"| ✅ Match | {len(by_status['match'])} | Description accurately reflects handler |")
    lines.append(f"| ⚠️ Incomplete | {len(by_status['incomplete'])} | Handler returns some but not all promised fields |")
    lines.append(f"| ❌ Mismatch | {len(by_status['mismatch'])} | Handler missing most promised fields |")
    lines.append(f"| 📝 Vague | {len(by_status['vague'])} | Description doesn't specify fields |")
    lines.append(f"| ❓ No Handler | {len(by_status['no_handler'])} | Handler implementation not found |")
    lines.append("")
    lines.append("---")
    lines.append("")
    
    # Detailed results by category
    by_category = defaultdict(list)
    for r in results:
        by_category[r.category].append(r)
    
    for category in sorted(by_category.keys()):
        category_results = by_category[category]
        
        # Only show problematic ones in detail
        problematic = [r for r in category_results if r.status in ('incomplete', 'mismatch', 'no_handler')]
        
        if not problematic:
            continue
        
        lines.append(f"## {category.upper()} Operations")
        lines.append("")
        
        for result in problematic:
            status_emoji = {
                'match': '✅',
                'incomplete': '⚠️',
                'mismatch': '❌',
                'vague': '📝',
                'no_handler': '❓'
            }.get(result.status, '❔')
            
            lines.append(f"### {status_emoji} `{result.operation_id}`")
            lines.append("")
            lines.append(f"**Description**: {result.description}")
            lines.append("")
            
            if result.promised_fields:
                lines.append(f"**Promised**: {', '.join(sorted(result.promised_fields))}")
            
            if result.actual_fields:
                lines.append(f"**Returns**: {', '.join(sorted(result.actual_fields)[:15])}")
            
            if result.missing_fields:
                lines.append(f"**Missing**: {', '.join(sorted(result.missing_fields))}")
            
            lines.append("")
            lines.append("---")
            lines.append("")
    
    return '\n'.join(lines)


def main():
    import sys
    
    parser = argparse.ArgumentParser(description="Audit VMware operation descriptions")
    parser.add_argument('--report', '-r', help="Output markdown report file")
    args = parser.parse_args()
    
    print("=" * 70, file=sys.stderr)
    print("VMware Operation Description Audit", file=sys.stderr)
    print("=" * 70, file=sys.stderr)
    print("", file=sys.stderr)
    
    operations = parse_operations()
    handlers = parse_handlers()
    results = audit_operations(operations, handlers)
    
    # Generate report
    report = generate_markdown_report(results)
    
    if args.report:
        output_path = Path(args.report)
        output_path.write_text(report)
        print(f"\n📄 Report written to: {output_path}", file=sys.stderr)
    else:
        print("\n" + report)
    
    # Summary
    by_status = defaultdict(int)
    for r in results:
        by_status[r.status] += 1
    
    print("\n" + "=" * 70, file=sys.stderr)
    print("Summary", file=sys.stderr)
    print("=" * 70, file=sys.stderr)
    for status, count in sorted(by_status.items()):
        print(f"  {status}: {count}", file=sys.stderr)


if __name__ == '__main__':
    import sys
    main()

