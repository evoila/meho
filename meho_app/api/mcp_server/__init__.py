# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""MCP Server module -- exposes curated tools for external AI agents."""

from meho_app.api.mcp_server.server import get_mcp_http_app

__all__ = ["get_mcp_http_app"]
