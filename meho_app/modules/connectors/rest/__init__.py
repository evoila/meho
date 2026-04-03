# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
REST/OpenAPI connector type.

This connector type handles REST APIs with OpenAPI specification discovery.
It provides:
- OpenAPI spec parsing and endpoint discovery
- Session-based authentication (login/refresh)
- Generic HTTP client for API calls
"""


# Lazy imports to avoid loading heavy dependencies upfront

from typing import Any


def __getattr__(name: str) -> Any:
    """Lazy import for REST connector components."""
    if name == "OpenAPIParser":
        from meho_app.modules.connectors.rest.spec_parser import OpenAPIParser

        return OpenAPIParser
    elif name == "GenericHTTPClient":
        from meho_app.modules.connectors.rest.http_client import GenericHTTPClient

        return GenericHTTPClient
    elif name == "SessionManager":
        from meho_app.modules.connectors.rest.session_manager import SessionManager

        return SessionManager
    elif name == "EndpointDescriptorModel":
        from meho_app.modules.connectors.rest.models import EndpointDescriptorModel

        return EndpointDescriptorModel
    elif name == "OpenAPISpecModel":
        from meho_app.modules.connectors.rest.models import OpenAPISpecModel

        return OpenAPISpecModel
    elif name in (
        "EndpointDescriptor",
        "EndpointDescriptorCreate",
        "EndpointUpdate",
        "EndpointFilter",
    ):
        from meho_app.modules.connectors.rest import schemas

        return getattr(schemas, name)
    elif name in ("EndpointDescriptorRepository", "OpenAPISpecRepository"):
        from meho_app.modules.connectors.rest import repository

        return getattr(repository, name)
    elif name in (
        "RESTConnectorService",
        "OpenAPIService",
        "get_openapi_service",
        "get_rest_connector_service",
    ):
        from meho_app.modules.connectors.rest import service

        return getattr(service, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # Schemas
    "EndpointDescriptor",
    "EndpointDescriptorCreate",
    # Models
    "EndpointDescriptorModel",
    # Repositories
    "EndpointDescriptorRepository",
    "EndpointFilter",
    "EndpointUpdate",
    # HTTP
    "GenericHTTPClient",
    # Parser
    "OpenAPIParser",
    "OpenAPIService",
    "OpenAPISpecModel",
    "OpenAPISpecRepository",
    # Service
    "RESTConnectorService",
    "SessionManager",
    "get_openapi_service",
    "get_rest_connector_service",
]
