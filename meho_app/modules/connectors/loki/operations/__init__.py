# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Loki Operations - Combined from Category Files

This module imports and combines all operation definitions from category files.

Categories:
- log_search: Search, errors, context, volume, patterns (5 operations)
- discovery: Labels and label values (2 operations)
- query: LogQL escape hatch (1 operation)

Total: 8 operations
"""

from .discovery import DISCOVERY_OPERATIONS
from .log_search import LOG_SEARCH_OPERATIONS
from .query import QUERY_OPERATIONS

# Version for auto-sync on startup
# Increment this when operations are added or significantly changed
# Format: YYYY.MM.DD.revision
LOKI_OPERATIONS_VERSION = "2026.03.05.1"

# Combined list of all Loki operations
LOKI_OPERATIONS = LOG_SEARCH_OPERATIONS + DISCOVERY_OPERATIONS + QUERY_OPERATIONS

__all__ = [
    "DISCOVERY_OPERATIONS",
    "LOG_SEARCH_OPERATIONS",
    "LOKI_OPERATIONS",
    "LOKI_OPERATIONS_VERSION",
    "QUERY_OPERATIONS",
]
