# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
SQLAlchemy models for the Connectors module.

This file contains:
- ConnectorModel: Core connector configuration
- UserCredentialModel: User-specific credentials for connectors
- ConnectorOperationModel: Typed connector operations (VMware, K8s, etc.)
- ConnectorTypeModel: Typed connector entity types
- EventRegistrationModel: Per-connector event registrations with HMAC secrets
- EventHistoryModel: Event audit log

Note: Protocol-specific models (Endpoint, SOAP operations, etc.) are in their
respective submodules (rest/, soap/, etc.).
"""

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import TIMESTAMP, Boolean, Column, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from meho_app.database import Base


class ProtocolType(enum.Enum):
    """Supported API protocols (legacy, use ConnectorType instead)"""

    REST = "rest"  # OpenAPI/REST APIs
    GRAPHQL = "graphql"  # GraphQL APIs
    GRPC = "grpc"  # gRPC services
    SOAP = "soap"  # SOAP/WSDL services


class ConnectorType(enum.Enum):
    """
    Connector implementation types.

    Determines which connector implementation is used:
    - REST: OpenAPI/REST via HTTP client
    - SOAP: WSDL/SOAP via zeep
    - VMWARE: vSphere via pyvmomi
    - PROXMOX: Proxmox VE via proxmoxer
    - GRAPHQL: GraphQL via HTTP (future)
    - GRPC: gRPC (future)
    - KUBERNETES: Kubernetes API (future)
    """

    REST = "rest"
    SOAP = "soap"
    VMWARE = "vmware"
    PROXMOX = "proxmox"
    GRAPHQL = "graphql"
    GRPC = "grpc"
    KUBERNETES = "kubernetes"


class ConnectorModel(Base):  # type: ignore[misc,valid-type]
    """
    API connector configuration.

    Represents a connection to an external system (vCenter, REST API, SOAP service, etc.).
    The connector_type determines which implementation handles operations.
    """

    __tablename__ = "connector"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)

    # Routing description for orchestrator agent
    # Describes what this connector manages, used by LLM for routing decisions
    routing_description = Column(Text, nullable=True)

    base_url = Column(String, nullable=False)

    # Connector type - determines which implementation handles this connector
    connector_type = Column(String, nullable=False, default="rest")

    # Skill file name for SpecialistAgent (e.g., "custom_crm.md"). None = use type-level default
    skill_name = Column(String, nullable=True)

    # Generated skill from pipeline (overwritten on each re-upload/regeneration)
    generated_skill = Column(Text, nullable=True)
    # Custom skill written by operator via UI (NEVER overwritten by pipeline)
    custom_skill = Column(Text, nullable=True)
    # Quality score (1-5 stars) based on operation metadata completeness
    skill_quality_score = Column(Integer, nullable=True)

    # Protocol-specific configuration (varies by connector_type)
    # REST: { "openapi_url": "..." }
    # GraphQL: { "endpoint_url": "...", "introspection_enabled": true }
    # gRPC: { "server_address": "...", "proto_source": "file|reflection" }
    # SOAP: { "wsdl_url": "...", "auth_type": "basic|session|ws_security" }
    # VMware: { "vcenter_host": "...", "port": 443, "disable_ssl_verification": false }
    protocol_config = Column(JSONB, nullable=True)

    # Authentication
    auth_type = Column(String, nullable=False)  # API_KEY, OAUTH2, BASIC, NONE, SESSION
    auth_config = Column(JSONB, nullable=False, default=dict)  # Encrypted auth details
    credential_strategy = Column(
        String, nullable=False, default="SYSTEM"
    )  # SYSTEM or USER_PROVIDED

    # Session-based authentication configuration
    login_url = Column(String, nullable=True)  # Login endpoint path (e.g., /api/v1/auth/login)
    login_method = Column(String, nullable=True)  # HTTP method for login (POST, GET)
    login_config = Column(JSONB, nullable=True)  # Login request/response configuration

    # Safety Policies
    allowed_methods = Column(
        JSONB, nullable=False, default=lambda: ["GET", "POST", "PUT", "PATCH", "DELETE"]
    )
    blocked_methods = Column(JSONB, nullable=False, default=list)
    default_safety_level = Column(String, nullable=False, default="safe")

    # Related connectors for cross-connector topology correlation
    # E.g., K8s connector related to GCP connector that hosts it
    # This enables automatic SAME_AS discovery between entities in related connectors
    related_connector_ids = Column(JSONB, nullable=True, default=list)

    # Phase 75: Whether automated sessions (events/tasks) can use this connector
    automation_enabled = Column(Boolean, nullable=False, default=True, server_default="true")

    # Phase 103: HMAC-SHA256 webhook signature verification secret
    # When set, inbound webhooks must include a valid X-Webhook-Signature header.
    # When None, webhooks are accepted without signature verification (backward compatible).
    webhook_secret = Column(String(256), nullable=True, default=None)

    # Reference to this connector as a topology entity
    # Enables correlation between REST/SOAP targets and discovered infrastructure
    topology_entity_id = Column(
        UUID(as_uuid=True), ForeignKey("topology_entities.id", ondelete="SET NULL"), nullable=True
    )

    # Metadata
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(
        TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    # Relationships - these will be defined via back_populates in the respective modules
    # We use string references to avoid circular imports
    specs = relationship(
        "OpenAPISpecModel", back_populates="connector", cascade="all, delete-orphan"
    )
    endpoints = relationship(
        "EndpointDescriptorModel", back_populates="connector", cascade="all, delete-orphan"
    )
    user_credentials = relationship(
        "UserCredentialModel", back_populates="connector", cascade="all, delete-orphan"
    )
    soap_operations = relationship(
        "SoapOperationDescriptorModel", back_populates="connector", cascade="all, delete-orphan"
    )
    soap_types = relationship(
        "SoapTypeDescriptorModel", back_populates="connector", cascade="all, delete-orphan"
    )
    typed_operations = relationship(
        "ConnectorOperationModel", back_populates="connector", cascade="all, delete-orphan"
    )
    typed_types = relationship(
        "ConnectorTypeModel", back_populates="connector", cascade="all, delete-orphan"
    )
    connector_memories = relationship(
        "ConnectorMemoryModel", back_populates="connector", cascade="all, delete-orphan"
    )
    event_registrations = relationship(
        "EventRegistrationModel", back_populates="connector", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_connector_tenant_name", "tenant_id", "name"),)


class UserCredentialModel(Base):  # type: ignore[misc,valid-type]
    """
    User-specific credentials for connectors.

    Stores encrypted credentials for USER_PROVIDED credential strategy.
    Supports password-based, API key, OAuth2, and session-based authentication.
    """

    __tablename__ = "user_connector_credential"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String, nullable=False, index=True)
    connector_id = Column(UUID(as_uuid=True), ForeignKey("connector.id"), nullable=False)

    # Encrypted credentials
    credential_type = Column(String, nullable=False)  # PASSWORD, API_KEY, OAUTH2_TOKEN, SESSION
    encrypted_credentials = Column(Text, nullable=False)

    # OAuth2 specific
    oauth2_refresh_token = Column(Text, nullable=True)
    oauth2_token_expires_at = Column(TIMESTAMP(timezone=True), nullable=True)

    # Session-based auth state (for SESSION auth type)
    session_token = Column(Text, nullable=True)  # Current session token (encrypted)
    session_token_expires_at = Column(TIMESTAMP(timezone=True), nullable=True)  # When session expires
    session_refresh_token = Column(Text, nullable=True)  # Refresh token (encrypted)
    session_refresh_expires_at = Column(TIMESTAMP(timezone=True), nullable=True)  # Refresh token expiry
    session_state = Column(String, nullable=True)  # LOGGED_OUT, LOGGED_IN, EXPIRED

    # Metadata
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(
        TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )
    last_used_at = Column(TIMESTAMP(timezone=True), nullable=True)

    # Phase 75: Credential health tracking
    credential_health = Column(String(20), nullable=True)  # "healthy", "unhealthy", "expired", null=unknown
    credential_health_message = Column(String(500), nullable=True)  # Human-readable health failure reason
    credential_health_checked_at = Column(TIMESTAMP(timezone=True), nullable=True)  # When health was last assessed

    connector = relationship("ConnectorModel", back_populates="user_credentials")

    __table_args__ = (Index("ix_user_connector", "user_id", "connector_id", unique=True),)


# Backward compatibility alias
UserConnectorCredentialModel = UserCredentialModel


class ConnectorOperationModel(Base):  # type: ignore[misc,valid-type]
    """
    Generic operations table for typed connectors (TASK-97).

    Used by VMware, Kubernetes, and other typed connectors.
    REST uses endpoint_descriptor table.
    SOAP uses soap_operation_descriptor table.

    This allows the agent to discover operations uniformly via
    search_operations regardless of connector type.
    """

    __tablename__ = "connector_operation"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connector_id = Column(UUID(as_uuid=True), ForeignKey("connector.id"), nullable=False)
    tenant_id = Column(String, nullable=False, index=True)

    # Operation identification
    operation_id = Column(String, nullable=False)  # "list_virtual_machines"
    name = Column(String, nullable=False)  # "List Virtual Machines"
    description = Column(Text, nullable=True)
    category = Column(String, nullable=True)  # "compute", "storage", "networking"

    # Parameters as JSONB array: [{name, type, required, description}]
    parameters = Column(JSONB, nullable=False, default=list)

    # Example usage
    example = Column(String, nullable=True)

    # Search optimization: pre-computed search content for BM25
    # Includes name, description, category, parameter names
    search_content = Column(Text, nullable=True)

    # Operation inheritance (Phase 65)
    # source: 'type' = inherited from type-level definition, 'custom' = instance-specific override
    source = Column(String(20), nullable=False, server_default="type")
    # If this is a custom override, points to the original type-level operation
    type_operation_id = Column(UUID(as_uuid=True), nullable=True)
    # NULL = use type-level default, False = disabled for this instance, True = explicitly enabled
    is_enabled_override = Column(Boolean, nullable=True)

    # Activation & Safety
    is_enabled = Column(Boolean, nullable=False, default=True)
    safety_level = Column(String, nullable=False, default="safe")  # safe, caution, dangerous
    requires_approval = Column(Boolean, nullable=False, default=False)

    # Response schema for Brain-Muscle architecture (TASK-161)
    # These fields help the LLM understand the structure of returned data
    # and prevent hallucination of entity names.
    response_entity_type = Column(String, nullable=True)  # "Namespace", "VirtualMachine"
    response_identifier_field = Column(String, nullable=True)  # "uid", "moref_id"
    response_display_name_field = Column(String, nullable=True)  # "name"

    # Timestamps
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(
        TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    connector = relationship("ConnectorModel", back_populates="typed_operations")

    __table_args__ = (
        Index("ix_conn_op_connector", "connector_id"),
        Index("ix_conn_op_connector_operation", "connector_id", "operation_id"),
        Index("ix_conn_op_tenant", "tenant_id"),
        Index("ix_conn_op_category", "connector_id", "category"),
    )


class ConnectorTypeModel(Base):  # type: ignore[misc,valid-type]
    """
    Generic types table for typed connectors (TASK-97).

    Used by VMware, Kubernetes, and other typed connectors.
    Stores entity type definitions for agent discovery.

    This allows the agent to understand what entities exist
    (VirtualMachine, Cluster, Pod, etc.) regardless of connector type.
    """

    __tablename__ = "connector_type"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connector_id = Column(UUID(as_uuid=True), ForeignKey("connector.id"), nullable=False)
    tenant_id = Column(String, nullable=False, index=True)

    # Type identification
    type_name = Column(String, nullable=False)  # "VirtualMachine"
    description = Column(Text, nullable=True)
    category = Column(String, nullable=True)  # "compute", "storage", "networking"

    # Properties as JSONB array: [{name, type, description}]
    properties = Column(JSONB, nullable=False, default=list)

    # Search optimization: pre-computed search content for BM25
    search_content = Column(Text, nullable=True)

    # Timestamps
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(
        TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    connector = relationship("ConnectorModel", back_populates="typed_types")

    __table_args__ = (
        Index("ix_conn_type_connector", "connector_id"),
        Index("ix_conn_type_connector_name", "connector_id", "type_name"),
        Index("ix_conn_type_tenant", "tenant_id"),
    )


class EventRegistrationModel(Base):  # type: ignore[misc,valid-type]
    """
    Per-connector event registration.

    Each registration gets its own UUID (used as the endpoint path), HMAC secret
    (Fernet-encrypted), Jinja2 prompt template, and rate-limit config. Multiple
    registrations per connector are supported (e.g., alerts vs. deployments).

    Tenant ID is denormalized from the connector for fast lookups -- never derived
    from the event payload.
    """

    __tablename__ = "event_registration"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connector_id = Column(
        UUID(as_uuid=True),
        ForeignKey("connector.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id = Column(String, nullable=False, index=True)  # denormalized from connector

    # Human-readable label (e.g., "Alertmanager Alerts")
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)

    # Security -- Fernet-encrypted HMAC-SHA256 secret
    encrypted_secret = Column(Text, nullable=False)

    # When False, HMAC signature verification is skipped (for systems like Jira
    # that cannot sign outgoing events). Secret is still generated for later use.
    require_signature = Column(Boolean, nullable=False, default=True)

    # Jinja2 template rendered with {{ payload }} to produce the investigation prompt
    prompt_template = Column(Text, nullable=False)

    # Rate limiting
    rate_limit_per_hour = Column(Integer, nullable=False, default=10)

    # Activation
    is_active = Column(Boolean, nullable=False, default=True)

    # Counters for quick dashboard stats (avoid COUNT queries)
    total_events_received = Column(Integer, nullable=False, default=0)
    total_events_processed = Column(Integer, nullable=False, default=0)
    total_events_deduplicated = Column(Integer, nullable=False, default=0)

    last_event_at = Column(TIMESTAMP(timezone=True), nullable=True)

    # Identity model -- who created this event registration and what it can access
    created_by_user_id = Column(String, nullable=True, index=True)
    allowed_connector_ids = Column(JSONB, nullable=True, default=None)
    delegation_active = Column(Boolean, nullable=False, default=True)

    # Phase 75: Notification targets for approval notifications
    notification_targets = Column(JSONB, nullable=True, default=None)  # [{"connector_id": "uuid", "contact": "email"}]

    # Response channel configuration (Phase 94: Slack/Teams response routing)
    response_config = Column(JSONB, nullable=True, default=None)

    # Timestamps
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(
        TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    # Relationships
    connector = relationship("ConnectorModel", back_populates="event_registrations")
    history = relationship(
        "EventHistoryModel", back_populates="registration", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_event_reg_connector", "connector_id"),
        Index("ix_event_reg_tenant", "tenant_id"),
    )


class EventHistoryModel(Base):  # type: ignore[misc,valid-type]
    """
    Audit log for every event received.

    Records the outcome (processed / deduplicated / rate_limited / failed) and
    optionally links to the chat session created when the event was processed.
    """

    __tablename__ = "event_history"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_registration_id = Column(
        UUID(as_uuid=True),
        ForeignKey("event_registration.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id = Column(String, nullable=False, index=True)

    # Outcome
    status = Column(String(20), nullable=False)  # processed, deduplicated, rate_limited, failed
    payload_hash = Column(String(64), nullable=False)  # SHA-256 hex digest of raw body
    payload_size_bytes = Column(Integer, nullable=False)

    # Link to created session (when status == "processed")
    session_id = Column(UUID(as_uuid=True), nullable=True)

    # Error details (when status == "failed")
    error_message = Column(Text, nullable=True)

    # How many duplicate deliveries were suppressed for this hash
    duplicates_suppressed = Column(Integer, nullable=False, default=0)

    # Timestamps
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    # Relationships
    registration = relationship("EventRegistrationModel", back_populates="history")

    __table_args__ = (
        Index("ix_event_history_registration", "event_registration_id"),
        Index("ix_event_history_created", "created_at"),
    )


__all__ = [
    "ConnectorModel",
    "ConnectorOperationModel",
    "ConnectorType",
    "ConnectorTypeModel",
    "EventHistoryModel",
    "EventRegistrationModel",
    "ProtocolType",
    "UserConnectorCredentialModel",
    "UserCredentialModel",
]

# Import related models to ensure SQLAlchemy can resolve string-based relationships
# These imports must come AFTER the class definitions to avoid circular imports
from meho_app.modules.connectors.rest import models as _rest_models  # noqa: E402, F401
from meho_app.modules.connectors.soap import models as _soap_models  # noqa: E402, F401
from meho_app.modules.memory import models as _memory_models  # noqa: E402, F401
