"""
Connector Router and Registry (TASK-97)

Routes operations to the right implementation based on connector_type.
This is the ONLY place in the codebase that switches on connector_type.
Everything else uses the generic BaseConnector interface.
"""

from typing import Dict, Any, Optional
from meho_openapi.connectors.base import BaseConnector, OperationResult

# Connector type registry
_connector_types: Dict[str, type] = {}


def register_connector_type(connector_type: str, connector_class: type) -> None:
    """Register a connector implementation for a connector type."""
    _connector_types[connector_type] = connector_class


async def get_connector_instance(
    connector_type: str,
    connector_id: str,
    config: Dict[str, Any],
    credentials: Dict[str, Any],
) -> BaseConnector:
    """
    Factory function to get the right connector implementation.
    
    This is the ONLY place in the codebase that switches on connector_type.
    Everything else uses the generic BaseConnector interface.
    
    Args:
        connector_type: Type of connector (vmware, rest, soap)
        connector_id: Unique connector identifier
        config: Connector configuration (host, port, etc.)
        credentials: User credentials (username, password)
    
    Returns:
        Configured connector instance
    
    Raises:
        ValueError: If connector_type is unknown
    """
    if connector_type == "vmware":
        # Lazy import to avoid loading pyvmomi when not needed
        from meho_openapi.connectors.vmware import VMwareConnector
        return VMwareConnector(connector_id, config, credentials)
    
    # Check registry for other connector types
    if connector_type in _connector_types:
        connector_class = _connector_types[connector_type]
        connector: BaseConnector = connector_class(connector_id, config, credentials)
        return connector
    
    raise ValueError(f"Unknown connector type: {connector_type}")


async def execute_connector_operation(
    connector_type: str,
    connector_id: str,
    config: Dict[str, Any],
    credentials: Dict[str, Any],
    operation_id: str,
    parameters: Dict[str, Any],
) -> OperationResult:
    """
    Execute an operation on any connector type.
    
    This is the main entry point called by API routes and agent tools.
    Handles connection lifecycle automatically.
    
    Args:
        connector_type: Type of connector (vmware, rest, soap)
        connector_id: Unique connector identifier
        config: Connector configuration
        credentials: User credentials
        operation_id: Operation to execute (e.g., "list_virtual_machines")
        parameters: Parameters for the operation
    
    Returns:
        OperationResult with success status and data/error
    """
    connector = await get_connector_instance(
        connector_type, connector_id, config, credentials
    )
    
    try:
        await connector.connect()
        result = await connector.execute(operation_id, parameters)
        return result
    finally:
        await connector.disconnect()


# Connection pool for keeping sessions alive across multiple calls
_connector_pool: Dict[tuple, BaseConnector] = {}


async def get_pooled_connector(
    connector_type: str,
    connector_id: str,
    user_id: str,
    config: Dict[str, Any],
    credentials: Dict[str, Any],
) -> BaseConnector:
    """
    Get or create a pooled connector instance.
    
    Keeps connections alive for session reuse (important for VMware).
    
    Args:
        connector_type: Type of connector
        connector_id: Connector ID
        user_id: User ID (for per-user sessions)
        config: Connector configuration
        credentials: User credentials
    
    Returns:
        Connected connector instance (cached or new)
    """
    cache_key = (connector_id, user_id)
    
    # Check pool
    if cache_key in _connector_pool:
        connector = _connector_pool[cache_key]
        if connector.is_connected:
            return connector
    
    # Create new connector
    connector = await get_connector_instance(
        connector_type, connector_id, config, credentials
    )
    await connector.connect()
    
    # Cache it
    _connector_pool[cache_key] = connector
    
    return connector


def clear_connector_pool(connector_id: Optional[str] = None) -> None:
    """Clear pooled connections (for cleanup or session expiry)."""
    global _connector_pool
    
    if connector_id:
        # Clear specific connector
        to_remove = [k for k in _connector_pool if k[0] == connector_id]
        for key in to_remove:
            _connector_pool.pop(key, None)
    else:
        # Clear all
        _connector_pool.clear()


__all__ = [
    "BaseConnector",
    "OperationResult",
    "get_connector_instance",
    "execute_connector_operation",
    "get_pooled_connector",
    "clear_connector_pool",
    "register_connector_type",
]

