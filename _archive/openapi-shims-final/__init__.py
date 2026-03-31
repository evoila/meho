"""
OpenAPI module - BACKWARD COMPATIBILITY SHIM.

DEPRECATED: This module has been refactored into meho_app.modules.connectors.
All actual implementations now live in:
- meho_app.modules.connectors.rest - REST/OpenAPI connector
- meho_app.modules.connectors.soap - SOAP/WSDL connector
- meho_app.modules.connectors.vmware - VMware connector

This module re-exports from the new locations for backward compatibility.
New code should import directly from meho_app.modules.connectors.

Usage (deprecated):
    from meho_app.modules.openapi import OpenAPIService, get_openapi_service
    
Preferred:
    from meho_app.modules.connectors.rest import RESTConnectorService
"""
from .service import OpenAPIService, get_openapi_service
from .routes import router
from .schemas import (
    Connector,
    ConnectorCreate,
    ConnectorUpdate,
    EndpointDescriptor,
    UserCredential,
)

__all__ = [
    "OpenAPIService",
    "get_openapi_service",
    "router",
    "Connector",
    "ConnectorCreate",
    "ConnectorUpdate",
    "EndpointDescriptor",
    "UserCredential",
]
