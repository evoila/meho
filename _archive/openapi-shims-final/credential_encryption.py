"""
Backward compatibility shim for credential encryption.

This module has been moved to meho_app.modules.connectors.credential_encryption.
This file re-exports it for backward compatibility.
"""
# Re-export from the new location
from meho_app.modules.connectors.credential_encryption import CredentialEncryption

__all__ = ["CredentialEncryption"]
