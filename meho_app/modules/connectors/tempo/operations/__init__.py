# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Tempo Operations - Combined from Category Files

This module imports and combines all operation definitions from category files.

Categories:
- traces: Search, get_trace, get_span_details, get_slow_traces, get_error_traces (5 operations)
- graph: Service graph and trace-derived metrics (2 operations)
- discovery: Tags and tag values (2 operations)
- query: TraceQL escape hatch (1 operation)

Total: 10 operations
"""

from .discovery import DISCOVERY_OPERATIONS
from .graph import GRAPH_OPERATIONS
from .query import QUERY_OPERATIONS
from .traces import TRACE_OPERATIONS

# Version for auto-sync on startup
# Increment this when operations are added or significantly changed
# Format: YYYY.MM.DD.revision
TEMPO_OPERATIONS_VERSION = "2026.03.05.1"

# Combined list of all Tempo operations
TEMPO_OPERATIONS = TRACE_OPERATIONS + GRAPH_OPERATIONS + DISCOVERY_OPERATIONS + QUERY_OPERATIONS

__all__ = [
    "DISCOVERY_OPERATIONS",
    "GRAPH_OPERATIONS",
    "QUERY_OPERATIONS",
    "TEMPO_OPERATIONS",
    "TEMPO_OPERATIONS_VERSION",
    "TRACE_OPERATIONS",
]
