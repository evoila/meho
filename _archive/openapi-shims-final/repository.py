"""
Repository for OpenAPI service database operations.

NOTE: This file re-exports from the repositories/ package for backward compatibility.
New code should import directly from meho_app.modules.openapi.repositories.
"""
# Re-export all repositories from the new package location
from meho_app.modules.openapi.repositories import (
    ConnectorRepository,
    OpenAPISpecRepository,
    EndpointDescriptorRepository,
    SoapOperationRepository,
    SoapTypeRepository,
    ConnectorOperationRepository,
    ConnectorTypeRepository,
)

__all__ = [
    "ConnectorRepository",
    "OpenAPISpecRepository",
    "EndpointDescriptorRepository",
    "SoapOperationRepository",
    "SoapTypeRepository",
    "ConnectorOperationRepository",
    "ConnectorTypeRepository",
]
