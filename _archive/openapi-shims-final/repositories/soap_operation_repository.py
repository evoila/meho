"""
Repository for SOAP Operation Descriptor database operations.

NOTE: This file is a backward-compatibility shim.
The actual implementation is in meho_app.modules.connectors.soap.repository.
Import from meho_app.modules.connectors.soap.repository for new code.
"""
# Re-export from connectors for backward compatibility
from meho_app.modules.connectors.soap.repository import SoapOperationRepository

__all__ = ["SoapOperationRepository"]
