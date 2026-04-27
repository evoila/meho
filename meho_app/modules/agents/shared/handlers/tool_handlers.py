# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Tool Handlers for MEHO ReAct Graph (TASK-89)

IMPORTANT: This is a backward-compatible RE-EXPORT module.

The actual implementations have been split into:
- handlers/endpoint_handlers.py - REST endpoint operations
- handlers/knowledge_handlers.py - knowledge search, reduce_data, list_connectors
- handlers/operation_handlers.py - generic operations (REST, SOAP, VMware)

All existing imports from this module continue to work.
"""

# Re-export all handlers from the new package structure
from meho_app.modules.agents.shared.handlers import (
    # SOAP client cache
    SOAP_CLIENT_CACHE,
    call_endpoint_handler,
    call_operation_handler,
    list_connectors_handler,
    reduce_data_handler,
    # Registration
    register_default_tools,
    # Endpoint handlers
    search_endpoints_handler,
    # Knowledge handlers
    search_knowledge_handler,
    # Operation handlers
    search_operations_handler,
    search_types_handler,
)

# Backward compatibility: expose the SOAP cache under old name too
_soap_client_cache = SOAP_CLIENT_CACHE

__all__ = [
    # SOAP client cache
    "SOAP_CLIENT_CACHE",
    "_soap_client_cache",  # Backward compatibility
    "call_endpoint_handler",
    "call_operation_handler",
    "list_connectors_handler",
    "reduce_data_handler",
    # Registration
    "register_default_tools",
    # Endpoint handlers
    "search_endpoints_handler",
    # Knowledge handlers
    "search_knowledge_handler",
    # Operation handlers
    "search_operations_handler",
    "search_types_handler",
]
