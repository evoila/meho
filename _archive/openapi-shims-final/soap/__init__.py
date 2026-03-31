"""
Backward compatibility shim for SOAP connector.

This module has been moved to meho_app.modules.connectors.soap.
This file re-exports it for backward compatibility.
"""
# Re-export from the new location
from meho_app.modules.connectors.soap import (
    SOAPSchemaIngester,
    SOAPClient,
    SOAPOperation,
    SOAPConnectorConfig,
    SOAPAuthType,
    WSDLMetadata,
    SOAPProperty,
    SOAPTypeDefinition,
)

__all__ = [
    "SOAPSchemaIngester",
    "SOAPClient",
    "SOAPOperation",
    "SOAPConnectorConfig",
    "SOAPAuthType",
    "WSDLMetadata",
    "SOAPProperty",
    "SOAPTypeDefinition",
]
