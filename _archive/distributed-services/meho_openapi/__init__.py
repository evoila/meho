"""
MEHO OpenAPI Service - Dynamic API integration without custom code.

Exports:
    - Models: ConnectorModel, OpenAPISpecModel, EndpointDescriptorModel, ProtocolType
    - Schemas: ConnectorCreate, Connector, EndpointDescriptor, etc.
    - Repositories: ConnectorRepository, EndpointDescriptorRepository
    - Clients: GenericHTTPClient, ProtocolRouter
    - SOAP: SOAPSchemaIngester, SOAPClient, SOAPOperation (TASK-75)
"""
from meho_openapi.models import (
    Base,
    ConnectorModel,
    OpenAPISpecModel,
    EndpointDescriptorModel,
    UserConnectorCredentialModel,
    ProtocolType,
)
from meho_openapi.schemas import (
    ConnectorCreate,
    Connector,
    EndpointDescriptorCreate,
    EndpointDescriptor,
    EndpointFilter
)
from meho_openapi.repository import (
    ConnectorRepository,
    EndpointDescriptorRepository
)
from meho_openapi.http_client import GenericHTTPClient
from meho_openapi.protocol_router import ProtocolRouter, get_protocol_router

# SOAP support (TASK-75)
from meho_openapi.soap import (
    SOAPSchemaIngester,
    SOAPClient,
    SOAPOperation,
    SOAPConnectorConfig,
    SOAPAuthType,
)

__all__ = [
    # Models
    "Base",
    "ConnectorModel",
    "OpenAPISpecModel",
    "EndpointDescriptorModel",
    "UserConnectorCredentialModel",
    "ProtocolType",
    # Schemas
    "ConnectorCreate",
    "Connector",
    "EndpointDescriptorCreate",
    "EndpointDescriptor",
    "EndpointFilter",
    # Repositories
    "ConnectorRepository",
    "EndpointDescriptorRepository",
    # Clients
    "GenericHTTPClient",
    "ProtocolRouter",
    "get_protocol_router",
    # SOAP (TASK-75)
    "SOAPSchemaIngester",
    "SOAPClient",
    "SOAPOperation",
    "SOAPConnectorConfig",
    "SOAPAuthType",
]

