"""
SQLAlchemy models for OpenAPI service.

NOTE: This file is a backward-compatibility shim.
All models have been moved to the connectors module:

- ConnectorModel, UserCredentialModel → meho_app.modules.connectors.models
- OpenAPISpecModel, EndpointDescriptorModel → meho_app.modules.connectors.rest.models
- SoapOperationDescriptorModel, SoapTypeDescriptorModel → meho_app.modules.connectors.soap.db_models
- ConnectorOperationModel, ConnectorTypeModel → meho_app.modules.connectors.models
- ProtocolType → meho_app.modules.connectors.models

Import from meho_app.modules.connectors for new code.
"""
# Re-export from connectors module for backward compatibility
from meho_app.modules.connectors.models import (
    ProtocolType,
    ConnectorType,
    ConnectorModel,
    UserCredentialModel as UserConnectorCredentialModel,
    ConnectorOperationModel,
    ConnectorTypeModel,
)

# Re-export from connectors/rest for backward compatibility
from meho_app.modules.connectors.rest.models import (
    OpenAPISpecModel,
    EndpointDescriptorModel,
)

# Re-export from connectors/soap for backward compatibility
from meho_app.modules.connectors.soap.db_models import (
    SoapOperationDescriptorModel,
    SoapTypeDescriptorModel,
)

# Also import Base for alembic env.py compatibility
from meho_app.database import Base

__all__ = [
    "Base",
    "ProtocolType",
    "ConnectorType",
    "ConnectorModel",
    "UserConnectorCredentialModel",
    "OpenAPISpecModel",
    "EndpointDescriptorModel",
    "SoapOperationDescriptorModel",
    "SoapTypeDescriptorModel",
    "ConnectorOperationModel",
    "ConnectorTypeModel",
]
