# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
MCP Client Connector Module (Phase 93)

Provides integration with external MCP servers. MEHO discovers tools
via list_tools() and registers them as connector operations with
mcp_{server_name}_{tool_name} prefixing for namespace isolation.
"""

from meho_app.modules.connectors.mcp.connector import MCPConnector
from meho_app.modules.connectors.mcp.types import MCP_TYPES

__all__ = ["MCPConnector", "MCP_TYPES"]
