"""
Repository for Connector Entity Type database operations.

NOTE: This file is a backward-compatibility shim.
The actual implementation is in meho_app.modules.connectors.repositories.type_repository.
Import from meho_app.modules.connectors.repositories for new code.
"""
# Re-export from connectors for backward compatibility
from meho_app.modules.connectors.repositories.type_repository import ConnectorTypeRepository

__all__ = ["ConnectorTypeRepository"]
