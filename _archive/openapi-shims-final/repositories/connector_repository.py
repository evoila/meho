"""
Backward compatibility shim for ConnectorRepository.

This repository has been moved to meho_app.modules.connectors.repositories.connector_repository.
This file re-exports it for backward compatibility.
"""
# Re-export from the new location
from meho_app.modules.connectors.repositories.connector_repository import ConnectorRepository

__all__ = ["ConnectorRepository"]
