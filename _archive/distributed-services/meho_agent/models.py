"""
SQLAlchemy models for agent/workflow service.
"""
# mypy: disable-error-code="valid-type,misc,var-annotated"
from sqlalchemy import Column, String, Text, TIMESTAMP, Integer, Boolean, ForeignKey, Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID, JSONB, JSON
from sqlalchemy.orm import declarative_base, relationship
from datetime import datetime
import uuid
import enum

Base = declarative_base()


class PlanStatus(enum.Enum):
    """Agent plan execution status (formerly WorkflowStatus)"""
    PLANNING = "PLANNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class StepStatus(enum.Enum):
    """Individual step status"""
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class AgentPlanModel(Base):
    """Agent plan execution (ephemeral - for chat responses)
    
    Represents MEHO's execution plan for responding to a chat message.
    NOT a reusable workflow - this is ephemeral and tied to chat sessions.
    
    For reusable automation, see Recipe system (meho_agent/recipes/).
    """
    
    __tablename__ = "agent_plan"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(UUID(as_uuid=True), ForeignKey('chat_session.id'), nullable=True, index=True)
    tenant_id = Column(String, nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True)
    
    # Plan state
    status = Column(SQLEnum(PlanStatus), nullable=False)
    goal = Column(Text, nullable=False)
    plan_json = Column(JSONB, nullable=True)  # The generated plan
    current_step_index = Column(Integer, nullable=False, default=0)
    
    # Approval
    requires_approval = Column(Boolean, nullable=False, default=True)
    approved_at = Column(TIMESTAMP, nullable=True)
    
    # Metadata
    created_at = Column(TIMESTAMP, nullable=False, default=datetime.utcnow)
    updated_at = Column(TIMESTAMP, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    steps = relationship("AgentPlanStepModel", back_populates="agent_plan", cascade="all, delete-orphan")
    session = relationship("ChatSessionModel")


class AgentPlanStepModel(Base):
    """Individual agent plan step execution"""
    
    __tablename__ = "agent_plan_step"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_plan_id = Column(UUID(as_uuid=True), ForeignKey('agent_plan.id'), nullable=False, index=True)
    
    # Step details
    index = Column(Integer, nullable=False)  # Step order
    tool_name = Column(String, nullable=False)  # e.g., "call_endpoint"
    input_json = Column(JSONB, nullable=False)  # Tool arguments
    output_json = Column(JSONB, nullable=True)  # Tool result
    
    # Execution state
    status = Column(SQLEnum(StepStatus), nullable=False)
    error_message = Column(Text, nullable=True)
    started_at = Column(TIMESTAMP, nullable=True)
    finished_at = Column(TIMESTAMP, nullable=True)
    
    # Relationships
    agent_plan = relationship("AgentPlanModel", back_populates="steps")


# Expose plan models with clear names
# NOTE: HTTP routes still use "/workflows/*" for backwards compatibility
# but internally we call them AgentPlans to distinguish from WorkflowDefinitions


class ChatSessionModel(Base):
    """Chat session for conversation persistence"""
    
    __tablename__ = "chat_session"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(String, nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True)
    
    # Session metadata
    title = Column(String, nullable=True)  # Auto-generated or user-set
    created_at = Column(TIMESTAMP, nullable=False, default=datetime.utcnow)
    updated_at = Column(TIMESTAMP, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    messages = relationship("ChatMessageModel", back_populates="session", cascade="all, delete-orphan")


class ChatMessageModel(Base):
    """Individual message in a chat session"""
    
    __tablename__ = "chat_message"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(UUID(as_uuid=True), ForeignKey('chat_session.id'), nullable=False, index=True)
    
    # Message details
    role = Column(String, nullable=False)  # 'user' or 'assistant'
    content = Column(Text, nullable=False)  # Text content for backward compatibility
    
    # Full PydanticAI message structure (Session 69)
    # Stores complete message with tool calls, tool results, and all parts
    # Format: {"role": "assistant", "parts": [{"type": "tool_call", ...}, ...]}
    message_data = Column(JSON, nullable=True)
    
    # Link to agent plan (if message triggered a plan execution)
    agent_plan_id = Column(UUID(as_uuid=True), ForeignKey('agent_plan.id'), nullable=True)
    
    # Legacy field (keep for migration compatibility)
    workflow_id = Column(UUID(as_uuid=True), nullable=True)
    
    created_at = Column(TIMESTAMP, nullable=False, default=datetime.utcnow)
    
    # Relationships
    session = relationship("ChatSessionModel", back_populates="messages")
    agent_plan = relationship("AgentPlanModel")


# =============================================================================
# DEPRECATED: WorkflowDefinition models removed in Session 80
# Replaced by Recipe system below
# =============================================================================


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
    created_at = Column(TIMESTAMP, nullable=False, default=datetime.utcnow)
    updated_at = Column(TIMESTAMP, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Usage stats
    execution_count = Column(Integer, nullable=False, default=0)
    last_executed_at = Column(TIMESTAMP, nullable=True)
    
    # Sharing
    is_public = Column(Boolean, nullable=False, default=False)
    created_by = Column(String, nullable=True)
    
    # Relationships
    executions = relationship("RecipeExecutionModel", back_populates="recipe", cascade="all, delete-orphan")


class RecipeExecutionModel(Base):
    """
    A single execution of a recipe.
    
    Tracks when a recipe was run, with what parameters,
    and what the results were.
    """
    
    __tablename__ = "recipe_execution"
    
    # Identity
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    recipe_id = Column(UUID(as_uuid=True), ForeignKey('recipe.id'), nullable=False, index=True)
    tenant_id = Column(String, nullable=False, index=True)
    
    # Execution parameters
    parameter_values = Column(JSONB, nullable=False, default=dict)
    
    # Status
    status = Column(SQLEnum(RecipeExecutionStatus), nullable=False, default=RecipeExecutionStatus.PENDING)
    error_message = Column(Text, nullable=True)
    
    # Results
    result_count = Column(Integer, nullable=True)
    result_summary = Column(Text, nullable=True)
    aggregates = Column(JSONB, nullable=False, default=dict)
    
    # Performance
    started_at = Column(TIMESTAMP, nullable=True)
    completed_at = Column(TIMESTAMP, nullable=True)
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
    """
    
    __tablename__ = "tenant_agent_config"
    
    # Identity
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(String, nullable=False, unique=True, index=True)
    
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
    updated_at = Column(TIMESTAMP, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_at = Column(TIMESTAMP, nullable=False, default=datetime.utcnow)


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
    changed_at = Column(TIMESTAMP, nullable=False, default=datetime.utcnow)


# =============================================================================
# Approval System (TASK-76)
# Human-in-the-loop approval for risky API operations
# =============================================================================


class ApprovalStatus(enum.Enum):
    """Status of an approval request."""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class DangerLevel(enum.Enum):
    """Danger level of an API operation."""
    SAFE = "safe"           # GET, HEAD, OPTIONS - auto-approved
    CAUTION = "caution"     # Safe but sensitive - optional approval
    DANGEROUS = "dangerous" # POST, PUT, PATCH - requires approval
    CRITICAL = "critical"   # DELETE - requires approval + confirmation


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
    session_id = Column(UUID(as_uuid=True), ForeignKey('chat_session.id'), nullable=False, index=True)
    tenant_id = Column(String, nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True)
    
    # What needs approval
    tool_name = Column(String(100), nullable=False)  # e.g., "call_endpoint"
    tool_args = Column(JSONB, nullable=False)        # The actual arguments
    tool_args_hash = Column(String(64), nullable=False)  # SHA256 for deduplication
    
    # Human-readable context
    danger_level = Column(SQLEnum(DangerLevel), nullable=False)
    http_method = Column(String(10), nullable=True)  # GET, POST, DELETE, etc.
    endpoint_path = Column(String(500), nullable=True)  # /api/vcenter/vm/{vm}
    description = Column(Text, nullable=True)  # Human-readable action description
    impact_message = Column(Text, nullable=True)  # Warning about consequences
    
    # State for resume (so we can continue agent after approval)
    user_message = Column(Text, nullable=False)  # Original user request
    conversation_history = Column(JSONB, nullable=True)  # For agent resume
    
    # Status
    status = Column(SQLEnum(ApprovalStatus), nullable=False, default=ApprovalStatus.PENDING)
    
    # Decision info
    decided_by = Column(String, nullable=True)
    decided_at = Column(TIMESTAMP, nullable=True)
    decision_reason = Column(Text, nullable=True)
    
    # Timestamps
    created_at = Column(TIMESTAMP, nullable=False, default=datetime.utcnow)
    expires_at = Column(TIMESTAMP, nullable=True)  # Optional auto-expiry
    
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
    approval_request_id = Column(UUID(as_uuid=True), ForeignKey('approval_request.id'), nullable=True)
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
    
    # Metadata
    ip_address = Column(String(45), nullable=True)  # IPv6 max length
    user_agent = Column(Text, nullable=True)
    
    # Timestamp
    created_at = Column(TIMESTAMP, nullable=False, default=datetime.utcnow, index=True)

