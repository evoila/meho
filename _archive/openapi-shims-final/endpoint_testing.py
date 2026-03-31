"""
OpenAPI Endpoint Testing Service.

DEPRECATED: This module has been moved to meho_app.modules.connectors.rest.endpoint_testing
This file re-exports for backward compatibility.
"""
# Re-export from the new location
from meho_app.modules.connectors.rest.endpoint_testing import (
    TestEndpointResult,
    CallEndpointResult,
    OpenAPIService,
)

__all__ = [
    "TestEndpointResult",
    "CallEndpointResult",
    "OpenAPIService",
]
