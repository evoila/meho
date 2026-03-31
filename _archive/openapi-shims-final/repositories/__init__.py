"""
OpenAPI module repositories package.

Contains separate repository classes for different domain entities.

Note: ConnectorRepository has been moved to meho_app.modules.connectors.repositories.
It is re-exported here for backward compatibility.
"""
# Re-export from connectors module for backward compatibility
from meho_app.modules.connectors.repositories import ConnectorRepository

# OpenAPI-specific repositories
from meho_app.modules.openapi.repositories.spec_repository import OpenAPISpecRepository
from meho_app.modules.openapi.repositories.endpoint_repository import EndpointDescriptorRepository
from meho_app.modules.openapi.repositories.soap_operation_repository import SoapOperationRepository
from meho_app.modules.openapi.repositories.soap_type_repository import SoapTypeRepository
from meho_app.modules.openapi.repositories.operation_repository import ConnectorOperationRepository
from meho_app.modules.openapi.repositories.type_repository import ConnectorTypeRepository

__all__ = [
    "ConnectorRepository",  # Re-exported from connectors module
    "OpenAPISpecRepository", 
    "EndpointDescriptorRepository",
    "SoapOperationRepository",
    "SoapTypeRepository",
    "ConnectorOperationRepository",
    "ConnectorTypeRepository",
]

