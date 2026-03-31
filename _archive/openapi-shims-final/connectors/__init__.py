"""
Backward compatibility shim for connectors.

The connector infrastructure has been moved to meho_app.modules.connectors.
This file re-exports the necessary components for backward compatibility.
"""
# Re-export from the new location
from meho_app.modules.connectors.base import (
    BaseConnector,
    OperationResult,
    OperationDefinition,
    TypeDefinition,
)
from meho_app.modules.connectors.pool import (
    get_connector_instance,
    execute_connector_operation,
    get_pooled_connector,
    clear_connector_pool,
    register_connector_type,
)

__all__ = [
    "BaseConnector",
    "OperationResult",
    "OperationDefinition",
    "TypeDefinition",
    "get_connector_instance",
    "execute_connector_operation",
    "get_pooled_connector",
    "clear_connector_pool",
    "register_connector_type",
]
