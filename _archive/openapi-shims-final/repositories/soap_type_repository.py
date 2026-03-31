"""
Repository for SOAP Type Descriptor database operations.

NOTE: This file is a backward-compatibility shim.
The actual implementation is in meho_app.modules.connectors.soap.repository.
Import from meho_app.modules.connectors.soap.repository for new code.
"""
# Re-export from connectors for backward compatibility
from meho_app.modules.connectors.soap.repository import SoapTypeRepository

__all__ = ["SoapTypeRepository"]
