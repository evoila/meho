# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Pydantic schemas for the Connectors module.

Core connector schemas for CRUD operations.
Protocol-specific schemas (Endpoint, SOAP, etc.) are in their respective submodules.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ============================================================================
# Connector Schemas
# ============================================================================


class ConnectorCreate(BaseModel):
    """Request to create a connector."""

    tenant_id: str
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    routing_description: str | None = None  # For orchestrator LLM routing
    base_url: str

    # Connector type - determines which implementation handles this connector
    connector_type: Literal[
        "rest",
        "soap",
        "graphql",
        "grpc",
        "vmware",
        "proxmox",
        "kubernetes",
        "gcp",
        "prometheus",
        "loki",
        "tempo",
        "alertmanager",
        "jira",
        "confluence",
        "email",
        "argocd",
        "github",
        "mcp",
    ] = "rest"
    protocol_config: dict[str, Any] | None = None  # Type-specific configuration

    auth_type: Literal["API_KEY", "OAUTH2", "BASIC", "NONE", "SESSION"]
    auth_config: dict[str, Any] = Field(default_factory=dict)
    credential_strategy: Literal["SYSTEM", "USER_PROVIDED"] = "SYSTEM"

    # Session-based authentication configuration (for SESSION auth type)
    login_url: str | None = None  # e.g., "/api/v1/auth/login"
    login_method: str | None = "POST"  # POST or GET
    login_config: dict[str, Any] | None = None  # Login request/response configuration
    #
    # login_config structure:
    # {
    #     "login_auth_type": "basic" | "body",  # How to send credentials
    #     "login_headers": {...},                # Custom headers for login
    #     "body_template": {...},                # Template for login body
    #     "token_location": "header" | "cookie" | "body",
    #     "token_name": "X-Auth-Token",
    #     "token_path": "$.value",
    #     "header_name": "vmware-api-session-id",
    #     "session_duration_seconds": 3600,
    #     "refresh_url": "/api/v1/auth/refresh",
    #     "refresh_method": "POST",
    #     "refresh_token_path": "$.refreshToken",
    #     "refresh_token_expires_in": 86400,
    #     "refresh_body_template": {...}
    # }

    # Safety Policies
    allowed_methods: list[str] = Field(default=["GET", "POST", "PUT", "PATCH", "DELETE"])
    blocked_methods: list[str] = Field(default_factory=list)
    default_safety_level: Literal["safe", "caution", "dangerous"] = "safe"

    # Related connectors for cross-connector topology correlation
    # E.g., K8s connector's related_connector_ids includes the GCP connector that hosts it
    related_connector_ids: list[str] = Field(default_factory=list)


class Connector(ConnectorCreate):
    """Connector with ID and metadata."""

    id: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    # Reference to this connector as a topology entity (for cross-connector correlation)
    topology_entity_id: str | None = None

    # Skill fields (Phase 7 - Skill Editor UI)
    generated_skill: str | None = None
    custom_skill: str | None = None
    skill_quality_score: int | None = None
    skill_name: str | None = None

    model_config = ConfigDict(from_attributes=True)

    @field_validator("id", "topology_entity_id", mode="before")
    @classmethod
    def convert_uuid_to_str(cls, v: Any) -> str | None:
        """Convert UUID to string for id and topology_entity_id fields."""
        from uuid import UUID

        if v is None:
            return None
        if isinstance(v, UUID):
            return str(v)
        return str(v)

    @field_validator("auth_config", mode="before")
    @classmethod
    def ensure_auth_config_dict(cls, v: Any) -> dict[str, Any]:
        """Ensure auth_config is a dict, not None."""
        if v is None:
            return {}
        return dict(v) if v else {}


class ConnectorUpdate(BaseModel):
    """Update connector configuration."""

    name: str | None = None
    description: str | None = None
    routing_description: str | None = None  # For orchestrator LLM routing
    base_url: str | None = None

    connector_type: (
        Literal[
            "rest",
            "soap",
            "graphql",
            "grpc",
            "vmware",
            "proxmox",
            "kubernetes",
            "gcp",
            "prometheus",
            "loki",
            "tempo",
            "alertmanager",
            "jira",
            "confluence",
            "email",
            "argocd",
            "github",
            "mcp",
        ]
        | None
    ) = None
    protocol_config: dict[str, Any] | None = None

    auth_type: str | None = None
    auth_config: dict[str, Any] | None = None
    is_active: bool | None = None

    # Session auth configuration
    login_url: str | None = None
    login_method: str | None = None
    login_config: dict[str, Any] | None = None

    # Safety Policies
    allowed_methods: list[str] | None = None
    blocked_methods: list[str] | None = None
    default_safety_level: Literal["safe", "caution", "dangerous"] | None = None

    # Related connectors for cross-connector topology correlation
    related_connector_ids: list[str] | None = None

    # Topology entity reference (set automatically, not user-editable via API)
    topology_entity_id: str | None = None

    # Skill fields (Phase 7 - Skill Editor UI)
    custom_skill: str | None = None

    # Phase 75: automation toggle for automated sessions
    automation_enabled: bool | None = None


# ============================================================================
# User Credential Schemas
# ============================================================================


class UserCredentialProvide(BaseModel):
    """User provides their credentials for a connector."""

    connector_id: str
    credential_type: Literal["PASSWORD", "API_KEY", "OAUTH2_TOKEN", "SESSION"]
    credentials: dict[str, str]  # e.g., {"username": "...", "password": "..."}


class UserCredentialStatus(BaseModel):
    """Status of user's credentials."""

    connector_id: str
    connector_name: str
    has_credentials: bool
    credential_type: str | None = None
    is_active: bool
    last_used_at: datetime | None = None
    needs_refresh: bool = False


class UserCredentialCreate(BaseModel):
    """Create a user credential."""

    connector_id: str
    user_id: str
    credential_type: Literal["PASSWORD", "API_KEY", "OAUTH2_TOKEN", "SESSION"]
    credentials: dict[str, str]  # e.g., {"username": "...", "password": "..."}


class UserCredential(BaseModel):
    """User credential (without exposing encrypted data)."""

    id: str
    connector_id: str
    user_id: str
    credential_type: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


# ============================================================================
# VMware Connector Schemas
# ============================================================================


class CreateVMwareConnectorRequest(BaseModel):
    """Request to create a VMware vSphere connector."""

    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    vcenter_host: str = Field(..., description="vCenter Server hostname or IP")
    port: int = Field(default=443, description="vCenter Server port")
    disable_ssl_verification: bool = Field(
        default=False,
        description="Disable SSL certificate verification (not recommended for production)",
    )
    username: str = Field(..., description="vCenter username (e.g., administrator@vsphere.local)")
    password: str = Field(..., description="vCenter password")


class VMwareConnectorResponse(BaseModel):
    """Response after creating a VMware connector."""

    id: str
    name: str
    vcenter_host: str
    connector_type: str = "vmware"
    operations_registered: int
    types_registered: int
    message: str


# ============================================================================
# Proxmox Connector Schemas
# ============================================================================


class CreateProxmoxConnectorRequest(BaseModel):
    """Request to create a Proxmox VE connector."""

    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    host: str = Field(..., description="Proxmox VE hostname or IP")
    port: int = Field(default=8006, description="Proxmox VE API port")
    disable_ssl_verification: bool = Field(
        default=False,
        description="Disable SSL certificate verification (not recommended for production)",
    )
    # Authentication - either API token or username/password
    # API Token auth (recommended)
    api_token_id: str | None = Field(
        default=None, description="API token ID (e.g., user@realm!tokenname)"
    )
    api_token_secret: str | None = Field(default=None, description="API token secret")
    # Username/password auth
    username: str | None = Field(default=None, description="Username (e.g., root@pam)")
    password: str | None = Field(default=None, description="Password")


class ProxmoxConnectorResponse(BaseModel):
    """Response after creating a Proxmox connector."""

    id: str
    name: str
    host: str
    connector_type: str = "proxmox"
    operations_registered: int
    types_registered: int
    message: str


# ============================================================================
# Typed Connector Schemas (TASK-97: VMware/Kubernetes/etc)
# ============================================================================


class ConnectorOperationCreate(BaseModel):
    """Create a typed connector operation"""

    connector_id: str
    tenant_id: str
    operation_id: str  # e.g., "list_virtual_machines"
    name: str  # e.g., "List Virtual Machines"
    description: str | None = None
    category: str | None = None  # e.g., "compute", "storage"
    parameters: list[dict[str, Any]] = Field(default_factory=list)
    example: str | None = None
    search_content: str | None = None
    is_enabled: bool = True
    safety_level: Literal[
        "safe", "caution", "dangerous", "read", "write", "destructive", "auto"
    ] = "safe"
    requires_approval: bool = False

    # Response schema for Brain-Muscle architecture (TASK-161)
    # These fields help the LLM understand the structure of returned data
    response_entity_type: str | None = None  # e.g., "Namespace", "VirtualMachine"
    response_identifier_field: str | None = None  # e.g., "uid", "moref_id"
    response_display_name_field: str | None = None  # e.g., "name"


class ConnectorOperationDescriptor(ConnectorOperationCreate):
    """Typed connector operation with ID and timestamps"""

    id: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ConnectorOperationFilter(BaseModel):
    """Filter for searching connector operations"""

    category: str | None = None
    search: str | None = None
    is_enabled: bool | None = None
    safety_level: (
        Literal["safe", "caution", "dangerous", "read", "write", "destructive", "auto"] | None
    ) = None


class ConnectorEntityTypeCreate(BaseModel):
    """Create a typed connector entity type"""

    connector_id: str
    tenant_id: str
    type_name: str  # e.g., "VirtualMachine"
    description: str | None = None
    category: str | None = None  # e.g., "compute", "storage"
    properties: list[dict[str, Any]] = Field(default_factory=list)
    search_content: str | None = None


class ConnectorEntityType(ConnectorEntityTypeCreate):
    """Typed connector entity type with ID and timestamps"""

    id: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ConnectorEntityTypeFilter(BaseModel):
    """Filter for searching connector entity types"""

    category: str | None = None
    search: str | None = None
