# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Confluence Operations - Combined from Category Files.

This module imports and combines all operation definitions from category files.

Categories:
- search: search_pages, get_recent_changes, search_by_cql (3 operations)
- content: get_page, create_page, update_page, add_comment (4 operations)
- spaces: list_spaces (1 operation)

Total: 8 operations
"""

from .content import CONTENT_OPERATIONS
from .search import SEARCH_OPERATIONS
from .spaces import SPACE_OPERATIONS

# Version for auto-sync on startup
# Increment this when operations are added or significantly changed
CONFLUENCE_OPERATIONS_VERSION = "1.0.0"

# Combined list of all Confluence operations
CONFLUENCE_OPERATIONS = SEARCH_OPERATIONS + CONTENT_OPERATIONS + SPACE_OPERATIONS

# Operation IDs that require WRITE trust (used during sync registration)
# search_by_cql is WRITE because arbitrary CQL is an escape hatch
WRITE_OPERATIONS = {"search_by_cql", "create_page", "update_page", "add_comment"}

__all__ = [
    "CONFLUENCE_OPERATIONS",
    "CONFLUENCE_OPERATIONS_VERSION",
    "CONTENT_OPERATIONS",
    "SEARCH_OPERATIONS",
    "SPACE_OPERATIONS",
    "WRITE_OPERATIONS",
]
