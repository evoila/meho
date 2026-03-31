"""
OpenAPI module public service interface.

NOTE: This file is a backward-compatibility shim.
The actual implementation is in meho_app.modules.connectors.rest.service.
Import from meho_app.modules.connectors.rest.service for new code.
"""
# Re-export from connectors/rest for backward compatibility
from meho_app.modules.connectors.rest.service import (
    RESTConnectorService,
    OpenAPIService,
    get_openapi_service,
    get_rest_connector_service,
)

__all__ = [
    "RESTConnectorService",
    "OpenAPIService",
    "get_openapi_service",
    "get_rest_connector_service",
]
