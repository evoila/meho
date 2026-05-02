# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Connector management API package.

This package contains schemas, operations, and the main router for connector management.
"""

from meho_app.api.connectors.router import router
from meho_app.api.connectors.schemas import (
    # Core connector schemas
    CallSOAPRequest,
    CallSOAPResponse,
    ConnectorOperationResponse,
    ConnectorResponse,
    CreateConnectorRequest,
    CreateGCPConnectorRequest,
    CreateVMwareConnectorRequest,
    EndpointResponse,
    GCPConnectorResponse,
    IngestWSDLRequest,
    IngestWSDLResponse,
    SchemaTypeResponse,
    SOAPOperationResponse,
    SOAPTypeResponse,
    SyncOperationsResponse,
    TestAuthRequest,
    TestAuthResponse,
    TestConnectionRequest,
    TestConnectionResponse,
    TestEndpointRequest,
    TestEndpointResponse,
    UpdateConnectorRequest,
    UpdateEndpointRequest,
    VMwareConnectorResponse,
)

__all__ = [
    # Schemas
    "CallSOAPRequest",
    "CallSOAPResponse",
    "ConnectorOperationResponse",
    "ConnectorResponse",
    "CreateConnectorRequest",
    "CreateGCPConnectorRequest",
    "CreateVMwareConnectorRequest",
    "EndpointResponse",
    "GCPConnectorResponse",
    "IngestWSDLRequest",
    "IngestWSDLResponse",
    "SOAPOperationResponse",
    "SOAPTypeResponse",
    "SchemaTypeResponse",
    "SyncOperationsResponse",
    "TestAuthRequest",
    "TestAuthResponse",
    "TestConnectionRequest",
    "TestConnectionResponse",
    "TestEndpointRequest",
    "TestEndpointResponse",
    "UpdateConnectorRequest",
    "UpdateEndpointRequest",
    "VMwareConnectorResponse",
    # Router
    "router",
]
