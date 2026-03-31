"""Connector framework — abstract base, models, registry, and auth strategies.

Public API:
    BaseConnector       — ABC all connector types implement
    ConnectorConfig     — Pydantic model for YAML connector configs
    Operation           — Universal operation model stored in meho.db
    AuthConfig          — Authentication configuration
    TrustOverride       — Per-operation trust tier override
    register_connector  — Decorator to register a connector type
    get_connector_class — Look up registered connector class
    list_connector_types — List all registered connector types
    BearerAuth          — httpx Bearer token auth
    APIKeyAuth          — httpx API key auth (header or query)
    build_auth          — Factory to create httpx.Auth from config
"""

from meho_claude.core.connectors.auth import APIKeyAuth, BearerAuth, build_auth
from meho_claude.core.connectors.base import BaseConnector
from meho_claude.core.connectors.models import (
    AuthConfig,
    ConnectorConfig,
    Operation,
    TrustOverride,
)
from meho_claude.core.connectors.registry import (
    get_connector_class,
    list_connector_types,
    register_connector,
)

__all__ = [
    "APIKeyAuth",
    "AuthConfig",
    "BaseConnector",
    "BearerAuth",
    "ConnectorConfig",
    "Operation",
    "TrustOverride",
    "build_auth",
    "get_connector_class",
    "list_connector_types",
    "register_connector",
]

# Auto-import concrete connector modules to trigger @register_connector decorators.
try:
    from meho_claude.core.connectors import rest  # noqa: F401
except ImportError:
    pass

try:
    from meho_claude.core.connectors import kubernetes  # noqa: F401
except ImportError:
    pass  # kubernetes-asyncio not installed

try:
    from meho_claude.core.connectors import soap  # noqa: F401
except ImportError:
    pass  # zeep not installed

try:
    from meho_claude.core.connectors import vmware  # noqa: F401
except ImportError:
    pass  # pyvmomi not installed

try:
    from meho_claude.core.connectors import proxmox  # noqa: F401
except ImportError:
    pass  # proxmoxer not installed

try:
    from meho_claude.core.connectors import gcp  # noqa: F401
except ImportError:
    pass  # google-cloud-compute not installed
