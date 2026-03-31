"""
Backward compatibility shim for user credentials repository.

This repository has been moved to meho_app.modules.connectors.repositories.credential_repository.
This file re-exports it for backward compatibility.
"""
# Re-export from the new location
from meho_app.modules.connectors.repositories.credential_repository import (
    CredentialRepository as UserCredentialRepository,
)

__all__ = ["UserCredentialRepository"]
