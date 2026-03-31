# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Handler modules for MEHO agent tools.

Split from tool_handlers.py for better organization:
- endpoint_handlers: REST endpoint operations
- knowledge_handlers: knowledge search, reduce_data, list_connectors
- operation_handlers: generic operations (REST, SOAP, VMware)
- tracing: OTEL tracing utilities for rich observability
"""

from meho_app.modules.agents.shared.handlers.endpoint_handlers import (
    call_endpoint_handler,
    search_endpoints_handler,
)
from meho_app.modules.agents.shared.handlers.knowledge_handlers import (
    list_connectors_handler,
    reduce_data_handler,
    search_knowledge_handler,
)
from meho_app.modules.agents.shared.handlers.operation_handlers import (
    SOAP_CLIENT_CACHE,
    call_operation_handler,
    search_operations_handler,
    search_types_handler,
)
from meho_app.modules.agents.shared.handlers.tracing import (
    format_for_logging,
    sanitize_data,
    trace_operation_call,
    trace_llm_interaction,
    trace_sql_query,
    trace_topology_lookup,
    traced_tool_call,
)

__all__ = [
    "SOAP_CLIENT_CACHE",
    "call_endpoint_handler",
    "call_operation_handler",
    "format_for_logging",
    "list_connectors_handler",
    "reduce_data_handler",
    "sanitize_data",
    # Endpoint handlers
    "search_endpoints_handler",
    # Knowledge handlers
    "search_knowledge_handler",
    # Operation handlers
    "search_operations_handler",
    "search_types_handler",
    "trace_operation_call",
    "trace_llm_interaction",
    "trace_sql_query",
    "trace_topology_lookup",
    # Tracing utilities
    "traced_tool_call",
]


def register_default_tools(deps) -> None:
    """
    Register all default tool handlers.

    TASK-97: Uses generic tool names that work for ALL connector types.
    The agent doesn't need to know REST vs SOAP vs VMware.
    """
    # GENERIC TOOLS (work for all connector types)
    deps.register_tool("search_operations", search_operations_handler)
    deps.register_tool("call_operation", lambda d, a: call_operation_handler(d, a, state=None))
    deps.register_tool("search_types", search_types_handler)

    # Connector management
    deps.register_tool("list_connectors", list_connectors_handler)

    # Knowledge search
    deps.register_tool("search_knowledge", search_knowledge_handler)

    # Data reduction (Brain-Muscle architecture)
    deps.register_tool("reduce_data", reduce_data_handler)
