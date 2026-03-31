"""
SOAP Schema Ingester - Backward Compatibility Shim

NOTE: This module has been moved to meho_app.modules.connectors.soap.ingester.
This file re-exports it for backward compatibility.
"""
# Re-export from the new location
from meho_app.modules.connectors.soap.ingester import (
    SOAPSchemaIngester,
    SOAPSchemaIngesterError,
)

__all__ = [
    "SOAPSchemaIngester",
    "SOAPSchemaIngesterError",
]

