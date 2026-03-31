"""
Backward compatibility shim for BaseConnector.

The base connector has been moved to meho_app.modules.connectors.base.
This file re-exports it for backward compatibility.
"""
# Re-export from the new location
from meho_app.modules.connectors.base import (
    BaseConnector,
    OperationResult,
    OperationDefinition,
    TypeDefinition,
)

__all__ = [
    "BaseConnector",
    "OperationResult",
    "OperationDefinition",
    "TypeDefinition",
]
