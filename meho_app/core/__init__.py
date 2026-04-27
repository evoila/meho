# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
MEHO Core - Cross-cutting concerns and utilities.

Exports:
    - Config, get_config: Configuration management
    - UserContext, RequestContext: Authentication context
    - Permission, ROLE_PERMISSIONS, RequirePermission: RBAC enforcement
    - MehoError and subclasses: Error hierarchy
    - Observability: configure_observability, span
"""

from meho_app.core.auth_context import RequestContext, UserContext
from meho_app.core.config import Config, get_config, reset_config
from meho_app.core.errors import (
    AuthError,
    ConfigError,
    CredentialError,
    IngestionError,
    MehoError,
    NotFoundError,
    UpstreamApiError,
    ValidationError,
    VectorStoreError,
    WorkflowError,
)
from meho_app.core.observability import (
    clear_request_context,
    configure_observability,
    get_request_context,
    is_configured,
    set_request_context,
    span,
)
from meho_app.core.permissions import (
    ROLE_PERMISSIONS,
    Permission,
    RequirePermission,
    get_permissions_for_roles,
    has_permission,
)

__all__ = [
    "ROLE_PERMISSIONS",
    "AuthError",
    # Config
    "Config",
    "ConfigError",
    "CredentialError",
    "IngestionError",
    # Errors
    "MehoError",
    "NotFoundError",
    # Permissions (RBAC)
    "Permission",
    "RequestContext",
    "RequirePermission",
    "UpstreamApiError",
    # Auth
    "UserContext",
    "ValidationError",
    "VectorStoreError",
    "WorkflowError",
    "clear_request_context",
    # Observability
    "configure_observability",
    "get_config",
    "get_permissions_for_roles",
    "get_request_context",
    "has_permission",
    "is_configured",
    "reset_config",
    "set_request_context",
    "span",
]
