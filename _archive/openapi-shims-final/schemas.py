"""
Pydantic schemas for OpenAPI service.

Note: Schemas have been reorganized into the connectors module structure:
- Connector schemas → meho_app.modules.connectors.schemas
- Endpoint/Spec schemas → meho_app.modules.connectors.rest.schemas
- SOAP schemas → meho_app.modules.connectors.soap.schemas
- Typed connector schemas → (this file, moving to connectors/)

This file re-exports all schemas for backward compatibility.
"""
from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, List, Dict, Any, Literal
from datetime import datetime

# Re-export connector schemas for backward compatibility
from meho_app.modules.connectors.schemas import (
    ConnectorCreate,
    Connector,
    ConnectorUpdate,
    UserCredentialProvide,
    UserCredentialStatus,
    UserCredentialCreate,
    UserCredential,
    CreateVMwareConnectorRequest,
    VMwareConnectorResponse,
    # Typed connector schemas (TASK-97)
    ConnectorOperationCreate,
    ConnectorOperationDescriptor,
    ConnectorOperationFilter,
    ConnectorEntityTypeCreate,
    ConnectorEntityType,
    ConnectorEntityTypeFilter,
)

# Re-export REST/endpoint schemas for backward compatibility
from meho_app.modules.connectors.rest.schemas import (
    OpenAPISpecCreate,
    OpenAPISpec,
    EndpointDescriptorCreate,
    EndpointDescriptor,
    EndpointUpdate,
    EndpointFilter,
)

# Re-export SOAP schemas for backward compatibility
from meho_app.modules.connectors.soap.schemas import (
    SoapOperationDescriptorCreate,
    SoapOperationDescriptor,
    SoapOperationFilter,
    SoapPropertySchema,
    SoapTypeDescriptorCreate,
    SoapTypeDescriptor,
    SoapTypeFilter,
)
