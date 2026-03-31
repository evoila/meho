# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
SQLAlchemy models for agent service.
"""

# mypy: disable-error-code="valid-type,misc,var-annotated"
import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import TIMESTAMP, Boolean, Column, ForeignKey, Integer, String, Text
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import JSON, JSONB, UUID
from sqlalchemy.orm import relationship

from meho_app.database import Base

# =============================================================================
# Session Visibility (Phase 38: Group Session Foundation)
# =============================================================================


class SessionMode(enum.StrEnum):
    """Mode of a chat session.

    Controls what actions MEHO can take:
    - AGENT: Full agent mode with tool execution (default, existing behavior)
    - ASK: Knowledge-only Q&A mode, read-only connector data, no write/destructive actions

    Phase 65: Users toggle mode via chat input; mode is per-session with mid-session switching.
    """

    AGENT = "agent"
    ASK = "ask"


class SessionVisibility(enum.StrEnum):
    """Visibility level for chat sessions.

    Controls who can see and interact with a session:
    - PRIVATE: Only the session creator (default, current behavior)
    - GROUP: All users in the same tenant (v1.70: functionally same as TENANT)
    - TENANT: All users in the same tenant (v1.70: functionally same as GROUP)

    Visibility can only be upgraded (private -> group -> tenant), never downgraded.
    """

    PRIVATE = "private"
    GROUP = "group"
    TENANT = "tenant"


VISIBILITY_ORDER: dict[str, int] = {
    SessionVisibility.PRIVATE: 0,
    SessionVisibility.GROUP: 1,
    SessionVisibility.TENANT: 2,
}


def validate_visibility_upgrade(current: str, requested: str) -> bool:
    """Returns True if the visibility upgrade is valid (strictly increasing).

    Visibility can only be upgraded: private -> group -> tenant.
    Same-level and downgrade transitions return False.
    Unknown values always return False.

    Args:
        current: Current visibility value (e.g., "private")
        requested: Requested visibility value (e.g., "group")

    Returns:
        True if the requested visibility is strictly higher than current.
    """
    current_order = VISIBILITY_ORDER.get(current)
    requested_order = VISIBILITY_ORDER.get(requested)
    if current_order is None or requested_order is None:
        return False
    return requested_order > current_order


class ChatSessionModel(Base):
    """Chat session for conversation persistence"""

    __tablename__ = "chat_session"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(String, nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True)

    # Session metadata
    title = Column(String, nullable=True)  # Auto-generated or user-set
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(
        TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    # Session mode (Phase 65: Ask/Agent modes)
    session_mode = Column(String(10), nullable=False, server_default="agent")

    # Group session fields (Phase 38)
    visibility = Column(String(20), nullable=False, server_default="private")
    created_by_name = Column(String(255), nullable=True)  # Display name: "Alice" or "Alertmanager"
    trigger_source = Column(String(100), nullable=True)  # null=human, "event", "scheduled_task"

    # Relationships
    messages = relationship(
        "ChatMessageModel", back_populates="session", cascade="all, delete-orphan"
    )


class ChatMessageModel(Base):
    """Individual message in a chat session"""

    __tablename__ = "chat_message"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(
        UUID(as_uuid=True), ForeignKey("chat_session.id"), nullable=False, index=True
    )

    # Message details
    role = Column(String, nullable=False)  # 'user' or 'assistant'
    content = Column(Text, nullable=False)  # Text content for backward compatibility

    # Full PydanticAI message structure (Session 69)
    # Stores complete message with tool calls, tool results, and all parts
    # Format: {"role": "assistant", "parts": [{"type": "tool_call", ...}, ...]}
    message_data = Column(JSON, nullable=True)

    # War room sender attribution (Phase 39)
    sender_id = Column(String(255), nullable=True)
    sender_name = Column(String(255), nullable=True)

    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    # Relationships
    session = relationship("ChatSessionModel", back_populates="messages")


# =============================================================================
# Recipe System (Session 80)
# Reusable Q&A patterns that users can save and execute with parameters
# =============================================================================


class RecipeExecutionStatus(enum.Enum):
    """Status of a recipe execution."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class RecipeModel(Base):
    """
    A saved recipe - a reusable Q&A pattern.

    Recipes capture successful Q&A interactions and allow users
    to replay them with different parameter values.
    """

    __tablename__ = "recipe"

    # Identity
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(String, nullable=False, index=True)

    # Metadata
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    tags = Column(JSONB, nullable=False, default=list)

    # Source information
    connector_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    endpoint_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    # The original question that created this recipe
    original_question = Column(Text, nullable=False)

    # Parameters that users can customize (stored as JSON array)
    parameters = Column(JSONB, nullable=False, default=list)

    # The query template (stored as JSON object)
    query_template = Column(JSONB, nullable=False)

    # Interpretation prompt (how to present results)
    interpretation_prompt = Column(Text, nullable=True)

    # Timestamps
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(
        TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    # Usage stats
    execution_count = Column(Integer, nullable=False, default=0)
    last_executed_at = Column(TIMESTAMP(timezone=True), nullable=True)

    # Sharing
    is_public = Column(Boolean, nullable=False, default=False)
    created_by = Column(String, nullable=True)

    # Relationships
    executions = relationship(
        "RecipeExecutionModel", back_populates="recipe", cascade="all, delete-orphan"
    )


class RecipeExecutionModel(Base):
    """
    A single execution of a recipe.

    Tracks when a recipe was run, with what parameters,
    and what the results were.
    """

    __tablename__ = "recipe_execution"

    # Identity
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    recipe_id = Column(UUID(as_uuid=True), ForeignKey("recipe.id"), nullable=False, index=True)
    tenant_id = Column(String, nullable=False, index=True)

    # Execution parameters
    parameter_values = Column(JSONB, nullable=False, default=dict)

    # Status
    status = Column(
        SQLEnum(RecipeExecutionStatus), nullable=False, default=RecipeExecutionStatus.PENDING
    )
    error_message = Column(Text, nullable=True)

    # Results
    result_count = Column(Integer, nullable=True)
    result_summary = Column(Text, nullable=True)
    aggregates = Column(JSONB, nullable=False, default=dict)

    # Performance
    started_at = Column(TIMESTAMP(timezone=True), nullable=True)
    completed_at = Column(TIMESTAMP(timezone=True), nullable=True)
    duration_ms = Column(Integer, nullable=True)

    # Triggered by
    triggered_by = Column(String, nullable=True)

    # Relationships
    recipe = relationship("RecipeModel", back_populates="executions")


# =============================================================================
# Tenant Agent Configuration (TASK-77)
# Admin-configurable system context per tenant
# =============================================================================


class TenantAgentConfig(Base):
    """
    Tenant-specific agent configuration.

    Allows admins to customize MEHO's behavior for their installation:
    - installation_context: Custom context added to system prompt
    - model_override: Optional model override
    - temperature_override: Optional temperature override
    - features: Feature flags for the tenant

    TASK-77: Externalize Prompts & Models
    TASK-139: Extended for tenant management (Phase 4)
    TASK-139 Phase 8: Added email_domains for tenant discovery
    """

    __tablename__ = "tenant_agent_config"

    # Identity
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(String, nullable=False, unique=True, index=True)

    # Tenant management fields (TASK-139 Phase 4)
    display_name = Column(String(255), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    subscription_tier = Column(String(50), nullable=False, default="free")

    # Email domains for tenant discovery (TASK-139 Phase 8)
    # Example: ["acme.com", "acme.org"] - users with these email domains
    # will be directed to this tenant's Keycloak realm for SSO
    email_domains = Column(JSONB, nullable=True, default=list)

    # Quota limits (NULL = unlimited)
    max_connectors = Column(Integer, nullable=True)
    max_knowledge_chunks = Column(Integer, nullable=True)
    max_workflows_per_day = Column(Integer, nullable=True)

    # Admin-defined context - the key feature!
    # This is added to the system prompt under "## Your Environment"
    installation_context = Column(Text, nullable=True)

    # Optional overrides (NULL = use defaults)
    model_override = Column(String(100), nullable=True)
    temperature_override = Column(JSONB, nullable=True)  # Use JSONB for float

    # Feature flags (e.g., {"experimental_tools": true})
    features = Column(JSONB, nullable=False, default=dict)

    # Metadata
    updated_by = Column(String, nullable=True)
    updated_at = Column(
        TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


class TenantAgentConfigAudit(Base):
    """
    Audit log for tenant agent configuration changes.

    Tracks who changed what and when for security/compliance.
    """

    __tablename__ = "tenant_agent_config_audit"

    # Identity
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(String, nullable=False, index=True)

    # What changed
    field_changed = Column(String(100), nullable=False)
    old_value = Column(Text, nullable=True)
    new_value = Column(Text, nullable=True)

    # Who changed it
    changed_by = Column(String, nullable=False)
    changed_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


# =============================================================================
# Approval System (TASK-76)
# Human-in-the-loop approval for risky API operations
# =============================================================================


class ApprovalStatus(enum.StrEnum):
    """Status of an approval request."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class TrustTier(enum.StrEnum):
    """Three-tier trust classification for operations.

    Phase 5: Replaces the four-tier DangerLevel for runtime classification.
    DangerLevel is retained for backward compatibility with existing DB records.

    Mapping from DangerLevel:
      safe -> READ, caution -> WRITE, dangerous -> WRITE, critical -> DESTRUCTIVE
    """

    READ = "read"  # Auto-approved, no interruption
    WRITE = "write"  # Requires approval (yellow modal)
    DESTRUCTIVE = "destructive"  # Requires approval (red modal)


class DangerLevel(enum.StrEnum):
    """Danger level of an API operation."""

    SAFE = "safe"  # GET, HEAD, OPTIONS - auto-approved
    CAUTION = "caution"  # Safe but sensitive - optional approval
    DANGEROUS = "dangerous"  # POST, PUT, PATCH - requires approval
    CRITICAL = "critical"  # DELETE - requires approval + confirmation


# Phase 5: Mapping from old four-tier DangerLevel to new three-tier TrustTier
DANGER_LEVEL_TO_TRUST: dict[DangerLevel, TrustTier] = {
    DangerLevel.SAFE: TrustTier.READ,
    DangerLevel.CAUTION: TrustTier.WRITE,
    DangerLevel.DANGEROUS: TrustTier.WRITE,
    DangerLevel.CRITICAL: TrustTier.DESTRUCTIVE,
}


class ApprovalRequestModel(Base):
    """
    Pending approval request for a risky tool call.

    When an agent wants to execute a dangerous operation (POST, PUT, DELETE),
    the execution is paused and an approval request is created. The user
    must approve or reject before execution can continue.

    TASK-76: Approval Flow Architecture
    """

    __tablename__ = "approval_request"

    # Identity
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(
        UUID(as_uuid=True), ForeignKey("chat_session.id"), nullable=False, index=True
    )
    tenant_id = Column(String, nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True)

    # What needs approval
    tool_name = Column(String(100), nullable=False)  # e.g., "call_endpoint"
    tool_args = Column(JSONB, nullable=False)  # The actual arguments
    tool_args_hash = Column(String(64), nullable=False)  # SHA256 for deduplication

    # Human-readable context
    danger_level = Column(String(20), nullable=False)
    http_method = Column(String(10), nullable=True)  # GET, POST, DELETE, etc.
    endpoint_path = Column(String(500), nullable=True)  # /api/vcenter/vm/{vm}
    description = Column(Text, nullable=True)  # Human-readable action description
    impact_message = Column(Text, nullable=True)  # Warning about consequences

    # State for resume (so we can continue agent after approval)
    user_message = Column(Text, nullable=False)  # Original user request
    conversation_history = Column(JSONB, nullable=True)  # For agent resume

    # Status
    status = Column(String(20), nullable=False, default=ApprovalStatus.PENDING)

    # Decision info
    decided_by = Column(String, nullable=True)
    decided_at = Column(TIMESTAMP(timezone=True), nullable=True)
    decision_reason = Column(Text, nullable=True)

    # Timestamps
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    expires_at = Column(TIMESTAMP(timezone=True), nullable=True)  # Optional auto-expiry

    # Unique constraint: one pending approval per session + args combination
    __table_args__ = (
        # Unique pending approvals per session
        # (allows re-request after rejection)
    )


class ApprovalAuditModel(Base):
    """
    Audit log for all approval-related actions.

    Records every action: request created, approved, rejected, expired, executed.
    Used for compliance and debugging.

    TASK-76: Approval Flow Architecture
    """

    __tablename__ = "approval_audit"

    # Identity
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    approval_request_id = Column(
        UUID(as_uuid=True), ForeignKey("approval_request.id"), nullable=True
    )
    session_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    tenant_id = Column(String, nullable=False, index=True)

    # What happened
    action = Column(String(50), nullable=False)  # created, approved, rejected, expired, executed
    actor_id = Column(String, nullable=True)  # User or system that performed action

    # Details
    tool_name = Column(String(100), nullable=False)
    tool_args = Column(JSONB, nullable=True)
    danger_level = Column(String(20), nullable=True)

    # Request context (captured at time of action)
    http_method = Column(String(10), nullable=True)
    endpoint_path = Column(String(500), nullable=True)

    # Outcome (logged after tool execution for approved operations)
    # Phase 5: TRUST-05 - nullable because audit entries created at
    # request/approval time won't have outcome yet.
    outcome_status = Column(String(20), nullable=True)  # "success", "failure", None
    outcome_summary = Column(Text, nullable=True)  # Brief result description

    # Metadata
    ip_address = Column(String(45), nullable=True)  # IPv6 max length
    user_agent = Column(Text, nullable=True)

    # Timestamp
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC), index=True)
