# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
MCP entity type definitions.

Minimal type definitions for MCP server entities.
"""

from meho_app.modules.connectors.base import TypeDefinition

MCP_TYPES = [
    TypeDefinition(
        type_name="MCPServer",
        description="An external MCP server providing tools.",
        category="mcp",
        properties=[
            {"name": "name", "type": "string", "description": "Server name"},
            {"name": "url", "type": "string", "description": "Server URL"},
            {
                "name": "transport",
                "type": "string",
                "description": "Transport type (streamable_http or stdio)",
            },
            {
                "name": "tools_count",
                "type": "integer",
                "description": "Number of discovered tools",
            },
        ],
    )
]
