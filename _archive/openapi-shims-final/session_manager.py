"""
Backward compatibility shim for session manager.

This module has been moved to meho_app.modules.connectors.rest.session_manager.
This file re-exports it for backward compatibility.
"""
# Re-export from the new location
from meho_app.modules.connectors.rest.session_manager import SessionManager

__all__ = ["SessionManager"]
