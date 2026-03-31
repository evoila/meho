"""
SOAP Client - Backward Compatibility Shim

NOTE: This module has been moved to meho_app.modules.connectors.soap.client.
This file re-exports it for backward compatibility.
"""
# Re-export from the new location
from meho_app.modules.connectors.soap.client import (
    SOAPClient,
    VMwareSOAPClient,
)

__all__ = [
    "SOAPClient",
    "VMwareSOAPClient",
]

