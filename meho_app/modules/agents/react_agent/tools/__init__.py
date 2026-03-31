# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tool registry for React Agent.

All tools are registered here. The agent uses this registry
to resolve tool names to classes.

Exports:
    All 14 tool classes
    TOOL_REGISTRY: Maps tool names to classes
    get_tool_class: Get tool class by name
    create_tool: Create tool instance by name
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from meho_app.modules.agents.base.tool import BaseTool

from .call_operation import CallOperationTool
from .dns_resolve import DnsResolveTool
from .forget_memory import ForgetMemoryTool
from .http_probe import HttpProbeTool
from .invalidate_topology import InvalidateTopologyTool
from .list_connectors import ListConnectorsTool
from .lookup_topology import LookupTopologyTool
from .recall_memory import RecallMemoryTool
from .reduce_data import ReduceDataTool
from .search_knowledge import SearchKnowledgeTool
from .search_operations import SearchOperationsTool
from .search_types import SearchTypesTool
from .store_memory import StoreMemoryTool
from .tcp_probe import TcpProbeTool
from .tls_check import TlsCheckTool

if TYPE_CHECKING:
    pass

TOOL_REGISTRY: dict[str, type[BaseTool]] = {
    "list_connectors": ListConnectorsTool,
    "search_knowledge": SearchKnowledgeTool,
    "search_operations": SearchOperationsTool,
    "search_types": SearchTypesTool,
    "reduce_data": ReduceDataTool,
    "call_operation": CallOperationTool,
    "lookup_topology": LookupTopologyTool,
    "invalidate_topology": InvalidateTopologyTool,
    "store_memory": StoreMemoryTool,
    "forget_memory": ForgetMemoryTool,
    "recall_memory": RecallMemoryTool,
    "dns_resolve": DnsResolveTool,
    "tcp_probe": TcpProbeTool,
    "http_probe": HttpProbeTool,
    "tls_check": TlsCheckTool,
}


def get_tool_class(name: str) -> type[BaseTool]:
    """Get tool class by name.

    Args:
        name: Tool name (e.g., "list_connectors").

    Returns:
        Tool class.

    Raises:
        KeyError: If tool not found.
    """
    return TOOL_REGISTRY[name]


def create_tool(name: str) -> BaseTool:
    """Create tool instance by name.

    Args:
        name: Tool name (e.g., "list_connectors").

    Returns:
        Tool instance.

    Raises:
        KeyError: If tool not found.
    """
    return get_tool_class(name)()


__all__ = [
    "TOOL_REGISTRY",
    "CallOperationTool",
    "DnsResolveTool",
    "ForgetMemoryTool",
    "HttpProbeTool",
    "InvalidateTopologyTool",
    "ListConnectorsTool",
    "LookupTopologyTool",
    "RecallMemoryTool",
    "ReduceDataTool",
    "SearchKnowledgeTool",
    "SearchOperationsTool",
    "SearchTypesTool",
    "StoreMemoryTool",
    "TcpProbeTool",
    "TlsCheckTool",
    "create_tool",
    "get_tool_class",
]
