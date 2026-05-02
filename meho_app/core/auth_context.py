# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Authentication and authorization context for MEHO.

UserContext represents the current user and their permissions.
RequestContext represents the full context of an HTTP request.
"""

import uuid

from pydantic import BaseModel, Field


class UserContext(BaseModel):
    """
    User authentication and authorization context.

    Represents a user with their tenant, roles, and groups.
    Used throughout MEHO for ACL filtering and audit logging.
    """

    user_id: str = Field(..., description="Unique user identifier")
    name: str | None = Field(default=None, description="User display name from JWT")
    tenant_id: str | None = Field(default=None, description="Tenant identifier")
    system_id: str | None = Field(default=None, description="Current system context (optional)")
    roles: list[str] = Field(default_factory=list, description="User roles (e.g., 'admin', 'user')")
    groups: list[str] = Field(
        default_factory=list, description="User groups/contracts (e.g., 'contract:providerX')"
    )

    # Superadmin context switching (Phase 2 - TASK-140)
    original_user_id: str | None = Field(
        default=None, description="Original user ID when acting as superadmin in tenant context"
    )
    original_tenant_id: str | None = Field(
        default=None, description="Original tenant ID when acting as superadmin in tenant context"
    )
    acting_as_superadmin: bool = Field(
        default=False, description="True when superadmin is operating in a tenant context"
    )

    def has_role(self, role: str) -> bool:
        """
        Check if user has a specific role.

        Args:
            role: Role name to check

        Returns:
            True if user has the role, False otherwise
        """
        return role in self.roles

    def has_any_role(self, roles: list[str]) -> bool:
        """
        Check if user has any of the specified roles.

        Args:
            roles: List of role names

        Returns:
            True if user has at least one role, False otherwise
        """
        return any(role in self.roles for role in roles)

    def has_all_roles(self, roles: list[str]) -> bool:
        """
        Check if user has all of the specified roles.

        Args:
            roles: List of role names

        Returns:
            True if user has all roles, False otherwise
        """
        return all(role in self.roles for role in roles)

    def has_group(self, group: str) -> bool:
        """
        Check if user belongs to a specific group.

        Args:
            group: Group name to check

        Returns:
            True if user is in the group, False otherwise
        """
        return group in self.groups

    def has_any_group(self, groups: list[str]) -> bool:
        """
        Check if user belongs to any of the specified groups.

        Args:
            groups: List of group names

        Returns:
            True if user is in at least one group, False otherwise
        """
        return any(group in self.groups for group in groups)

    def is_admin(self) -> bool:
        """Check if user has admin role"""
        return self.has_role("admin")

    def is_global_admin(self) -> bool:
        """Check if user is a global admin (master realm with global_admin role)"""
        # When acting as superadmin in tenant context, check original tenant
        if self.acting_as_superadmin and self.original_tenant_id is not None:
            return self.has_role("global_admin") and (self.original_tenant_id == "master")
        return self.has_role("global_admin") and (
            self.tenant_id is None or self.tenant_id == "master"
        )

    def is_acting_in_tenant_context(self) -> bool:
        """Check if this is a superadmin acting in a tenant context"""
        return self.acting_as_superadmin and self.original_user_id is not None

    def get_audit_user_id(self) -> str:
        """Get the user ID for audit purposes (original ID if acting as superadmin)"""
        return self.original_user_id or self.user_id

    def get_audit_tenant_id(self) -> str | None:
        """Get the tenant ID for audit purposes (original tenant if acting as superadmin)"""
        return self.original_tenant_id if self.acting_as_superadmin else self.tenant_id


class RequestContext(BaseModel):
    """
    Full request context including user and tracing information.

    Used for logging, tracing, and audit across service boundaries.
    """

    user: UserContext = Field(..., description="User context")
    request_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()), description="Unique request identifier"
    )
    session_id: str | None = Field(default=None, description="Session identifier (if available)")
    trace_id: str | None = Field(default=None, description="Distributed tracing ID (if available)")

    def to_log_context(self) -> dict:
        """
        Convert to dictionary for structured logging.

        Returns:
            Dictionary with key context fields for logging
        """
        return {
            "user_id": self.user.user_id,
            "tenant_id": self.user.tenant_id,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "trace_id": self.trace_id,
        }
