# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
MCP tool to OperationDefinition conversion.

Converts MCP tool metadata into MEHO's OperationDefinition format with
mandatory mcp_{server_name}_{tool_name} prefixing for namespace isolation.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from meho_app.modules.connectors.base import OperationDefinition


def mcp_tool_to_operation(tool: Any, server_name: str) -> OperationDefinition:
    """
    Convert an MCP tool to a MEHO OperationDefinition.

    Args:
        tool: MCP Tool object from list_tools() response.
        server_name: Sanitized server name for prefixing.

    Returns:
        OperationDefinition with prefixed operation_id.
    """
    prefixed_id = f"mcp_{server_name}_{tool.name}"

    # Extract parameters from JSON Schema
    params: list[dict[str, Any]] = []
    input_schema = getattr(tool, "inputSchema", None) or {}
    properties = input_schema.get("properties", {})
    required_fields = input_schema.get("required", [])

    for param_name, param_schema in properties.items():
        params.append(
            {
                "name": param_name,
                "type": param_schema.get("type", "string"),
                "description": param_schema.get("description", ""),
                "required": param_name in required_fields,
            }
        )

    return OperationDefinition(
        operation_id=prefixed_id,
        name=tool.name,
        description=tool.description or "",
        category="mcp",
        parameters=params,
    )


def compute_safety_level(tool: Any) -> str:
    """
    Compute safety level from MCP tool annotations.

    Maps MCP tool annotations (readOnlyHint, destructiveHint) to MEHO
    safety levels per D-08.

    Args:
        tool: MCP Tool object with optional annotations.

    Returns:
        Safety level string: "safe", "caution", or "dangerous".
    """
    annotations = getattr(tool, "annotations", None)
    if annotations is None:
        return "safe"

    destructive = getattr(annotations, "destructiveHint", None)
    read_only = getattr(annotations, "readOnlyHint", None)

    if destructive is True:
        return "dangerous"
    if read_only is False and not destructive:
        return "caution"
    return "safe"


def compute_tools_hash(tools: list[Any]) -> str:
    """
    Compute a SHA-256 hash of tool names + descriptions for version tracking.

    Used by sync to detect when a server's tool set has changed.

    Args:
        tools: List of MCP Tool objects.

    Returns:
        Hex-encoded SHA-256 hash string.
    """
    entries = sorted(
        {"name": getattr(t, "name", ""), "description": getattr(t, "description", "") or ""}
        for t in tools
    )
    payload = json.dumps(entries, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()
