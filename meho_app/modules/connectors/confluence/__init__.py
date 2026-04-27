# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Confluence Connector Module.

Provides ConfluenceConnector class with 8 operations for Confluence Cloud REST API v2:
- Search: search_pages, get_recent_changes, search_by_cql
- Content: get_page, create_page, update_page, add_comment
- Spaces: list_spaces

Agent reads/writes markdown; ADF conversion is invisible.
CQL search uses v1 API; page CRUD uses v2 API.
"""

from meho_app.modules.connectors.confluence.connector import ConfluenceConnector
from meho_app.modules.connectors.confluence.operations import (
    CONFLUENCE_OPERATIONS,
    CONFLUENCE_OPERATIONS_VERSION,
    WRITE_OPERATIONS,
)
from meho_app.modules.connectors.confluence.sync import sync_confluence_operations_if_needed

__all__ = [
    "CONFLUENCE_OPERATIONS",
    "CONFLUENCE_OPERATIONS_VERSION",
    "WRITE_OPERATIONS",
    "ConfluenceConnector",
    "sync_confluence_operations_if_needed",
]
