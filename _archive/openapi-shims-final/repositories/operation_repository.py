"""
Repository for Connector Operation database operations.

NOTE: This file is a backward-compatibility shim.
The actual implementation is in meho_app.modules.connectors.repositories.operation_repository.
Import from meho_app.modules.connectors.repositories for new code.
"""
# Re-export from connectors for backward compatibility
from meho_app.modules.connectors.repositories.operation_repository import ConnectorOperationRepository

__all__ = ["ConnectorOperationRepository"]
