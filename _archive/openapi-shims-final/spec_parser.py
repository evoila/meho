"""
Backward compatibility shim for OpenAPI spec parser.

This module has been moved to meho_app.modules.connectors.rest.spec_parser.
This file re-exports it for backward compatibility.
"""
# Re-export from the new location
from meho_app.modules.connectors.rest.spec_parser import OpenAPIParser

__all__ = ["OpenAPIParser"]
