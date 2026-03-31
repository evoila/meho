"""
Protocol Router

NOTE: This file is a backward-compatibility shim.
The actual implementation is in meho_app.modules.connectors.router.
Import from meho_app.modules.connectors.router for new code.
"""
# Re-export from connectors for backward compatibility
from meho_app.modules.connectors.router import (
    ProtocolRouter,
    get_protocol_router,
)

__all__ = [
    "ProtocolRouter",
    "get_protocol_router",
]
