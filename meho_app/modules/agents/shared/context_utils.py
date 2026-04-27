# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Context utilities for multi-turn awareness.

Shared helper functions for building context strings used in LLM prompts
to inform agents about available cached data from previous queries.
"""

from __future__ import annotations

from typing import Any


def build_tables_context(cached_tables: dict[str, Any]) -> str:
    """Build context string about cached tables for prompts.

    Formats cached table information into a markdown-style string that can be
    included in LLM prompts to inform the agent about available cached data.

    Reference implementation from react_agent/nodes/reason_node.py:125-151

    Args:
        cached_tables: Dictionary mapping table names to their metadata.
            Expected format: {"table_name": {"row_count": N, "columns": [...]}, ...}

    Returns:
        Formatted string describing cached tables, or empty string if none.

    Example:
        >>> tables = {"namespaces": {"row_count": 30, "columns": ["name", "status"]}}
        >>> build_tables_context(tables)
        '## Cached Data Tables (from previous queries)\\n...'
    """
    if not cached_tables:
        return ""

    lines = [
        "## Cached Data Tables (from previous queries)",
        "Query these with reduce_data SQL instead of calling new operations:\n",
    ]

    for table_name, info in cached_tables.items():
        row_count = info.get("row_count", "?")
        columns = info.get("columns", [])
        if columns:
            col_str = ", ".join(columns[:6])
            if len(columns) > 6:
                col_str += ", ..."
        else:
            col_str = "no columns"
        lines.append(f"- **{table_name}**: {row_count} rows [{col_str}]")

    return "\n".join(lines)
