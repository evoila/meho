"""
Backward compatibility shim for HTTP client.

This module has been moved to meho_app.modules.connectors.rest.http_client.
This file re-exports it for backward compatibility.
"""
# Re-export from the new location
from meho_app.modules.connectors.rest.http_client import GenericHTTPClient

__all__ = ["GenericHTTPClient"]
