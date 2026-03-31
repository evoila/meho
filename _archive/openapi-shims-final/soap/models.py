"""
SOAP/WSDL Data Models - Backward Compatibility Shim

NOTE: This module has been moved to meho_app.modules.connectors.soap.models.
This file re-exports it for backward compatibility.
"""
# Re-export from the new location
from meho_app.modules.connectors.soap.models import (
    SOAPAuthType,
    SOAPStyle,
    WSDLMetadata,
    SOAPParameter,
    SOAPOperation,
    SOAPConnectorConfig,
    SOAPCallParams,
    SOAPResponse,
    SOAPProperty,
    SOAPTypeDefinition,
)

__all__ = [
    "SOAPAuthType",
    "SOAPStyle",
    "WSDLMetadata",
    "SOAPParameter",
    "SOAPOperation",
    "SOAPConnectorConfig",
    "SOAPCallParams",
    "SOAPResponse",
    "SOAPProperty",
    "SOAPTypeDefinition",
]

