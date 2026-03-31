"""
Repository for EndpointDescriptor database operations.

DEPRECATED: This module has been moved to meho_app.modules.connectors.rest.repository
This file re-exports for backward compatibility.
"""
# Re-export from the new location
from meho_app.modules.connectors.rest.repository import EndpointDescriptorRepository

__all__ = ["EndpointDescriptorRepository"]
