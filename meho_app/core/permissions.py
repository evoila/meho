# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Permission enforcement for MEHO API.

Defines permissions, role-permission mappings, and the RequirePermission
dependency for protecting API endpoints.

Usage:
    from meho_app.core.permissions import Permission, RequirePermission

    @router.post("/connectors")
    async def create_connector(
        request: CreateConnectorRequest,
        user: UserContext = Depends(RequirePermission(Permission.CONNECTOR_CREATE))
    ):
        ...
"""

from enum import StrEnum
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from meho_app.core.auth_context import UserContext


# Lazy import function to avoid circular import with meho_app.api.auth
def _get_current_user_dependency() -> Any:
    """Import get_current_user lazily to avoid circular import."""
    from meho_app.api.auth import get_current_user

    return get_current_user


class Permission(StrEnum):
    """All permissions in MEHO."""

    # Connector permissions
    CONNECTOR_READ = "connector:read"
    CONNECTOR_CREATE = "connector:create"
    CONNECTOR_UPDATE = "connector:update"
    CONNECTOR_DELETE = "connector:delete"

    # Knowledge permissions
    KNOWLEDGE_READ = "knowledge:read"
    KNOWLEDGE_INGEST = "knowledge:ingest"
    KNOWLEDGE_DELETE = "knowledge:delete"

    # Workflow/Recipe permissions
    WORKFLOW_READ = "workflow:read"
    WORKFLOW_EXECUTE = "workflow:execute"
    WORKFLOW_CREATE = "workflow:create"

    # Chat session permissions
    CHAT_OWN = "chat:own"
    CHAT_ALL_TENANT = "chat:all_tenant"

    # Admin permissions
    ADMIN_CONFIG = "admin:config"

    # Tenant management (global_admin only)
    TENANT_LIST = "tenant:list"
    TENANT_CREATE = "tenant:create"
    TENANT_UPDATE = "tenant:update"


# Role -> Permissions mapping
# Each role has a set of permissions it grants
ROLE_PERMISSIONS: dict[str, set[Permission]] = {
    "viewer": {
        Permission.CONNECTOR_READ,
        Permission.KNOWLEDGE_READ,
        Permission.WORKFLOW_READ,
        Permission.CHAT_OWN,
    },
    "user": {
        Permission.CONNECTOR_READ,
        Permission.KNOWLEDGE_READ,
        Permission.KNOWLEDGE_INGEST,
        Permission.WORKFLOW_READ,
        Permission.WORKFLOW_EXECUTE,
        Permission.CHAT_OWN,
    },
    "admin": {
        # All tenant-scoped permissions
        Permission.CONNECTOR_READ,
        Permission.CONNECTOR_CREATE,
        Permission.CONNECTOR_UPDATE,
        Permission.CONNECTOR_DELETE,
        Permission.KNOWLEDGE_READ,
        Permission.KNOWLEDGE_INGEST,
        Permission.KNOWLEDGE_DELETE,
        Permission.WORKFLOW_READ,
        Permission.WORKFLOW_EXECUTE,
        Permission.WORKFLOW_CREATE,
        Permission.CHAT_OWN,
        Permission.CHAT_ALL_TENANT,
        Permission.ADMIN_CONFIG,
    },
    "global_admin": {
        # All permissions including tenant management
        Permission.CONNECTOR_READ,
        Permission.CONNECTOR_CREATE,
        Permission.CONNECTOR_UPDATE,
        Permission.CONNECTOR_DELETE,
        Permission.KNOWLEDGE_READ,
        Permission.KNOWLEDGE_INGEST,
        Permission.KNOWLEDGE_DELETE,
        Permission.WORKFLOW_READ,
        Permission.WORKFLOW_EXECUTE,
        Permission.WORKFLOW_CREATE,
        Permission.CHAT_OWN,
        Permission.CHAT_ALL_TENANT,
        Permission.ADMIN_CONFIG,
        Permission.TENANT_LIST,
        Permission.TENANT_CREATE,
        Permission.TENANT_UPDATE,
    },
}


def get_permissions_for_roles(roles: list[str]) -> set[Permission]:
    """
    Get all permissions granted by a list of roles.

    Args:
        roles: List of role names

    Returns:
        Set of all permissions the roles grant
    """
    permissions: set[Permission] = set()
    for role in roles:
        role_perms = ROLE_PERMISSIONS.get(role, set())
        permissions.update(role_perms)
    return permissions


class RequirePermission:
    """
    FastAPI dependency for permission-based access control.

    Checks that the current user has the required permission(s).
    Global admins bypass all permission checks.

    Usage:
        @router.post("/connectors")
        async def create_connector(
            request: CreateConnectorRequest,
            user: UserContext = Depends(RequirePermission(Permission.CONNECTOR_CREATE))
        ):
            ...

        # Multiple permissions (all required):
        @router.post("/admin/dangerous")
        async def dangerous_action(
            user: UserContext = Depends(RequirePermission(
                Permission.ADMIN_CONFIG,
                Permission.CONNECTOR_DELETE
            ))
        ):
            ...
    """

    def __init__(self, *required_permissions: Permission) -> None:
        """
        Initialize with required permissions.

        Args:
            *required_permissions: One or more permissions required to access the endpoint
        """
        if not required_permissions:
            raise ValueError("At least one permission is required")
        self.required_permissions = required_permissions

    async def __call__(
        self,
        request: Request,
        credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer()),
    ) -> UserContext:
        """
        Check if user has required permissions.

        Args:
            request: The FastAPI request
            credentials: The bearer token credentials

        Returns:
            UserContext if authorized

        Raises:
            HTTPException: 403 if user lacks required permissions
        """
        # Get the current user using lazy import to avoid circular import
        get_current_user = _get_current_user_dependency()
        user: UserContext = await get_current_user(request, credentials)

        # Global admin bypasses all permission checks
        if user.is_global_admin():
            return user

        # Collect all permissions from user's roles
        user_permissions = get_permissions_for_roles(user.roles)

        # Check all required permissions
        for perm in self.required_permissions:
            if perm not in user_permissions:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Permission denied: {perm.value} required",
                )

        return user


def has_permission(user: UserContext, permission: Permission) -> bool:
    """
    Check if a user has a specific permission.

    Utility function for manual permission checks in code.

    Args:
        user: The user context
        permission: The permission to check

    Returns:
        True if user has the permission, False otherwise
    """
    if user.is_global_admin():
        return True

    user_permissions = get_permissions_for_roles(user.roles)
    return permission in user_permissions
