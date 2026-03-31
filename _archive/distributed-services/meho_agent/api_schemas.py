"""
API schemas for Agent HTTP Service.
"""
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional, Union
from datetime import datetime


# ============================================================================
# Request Schemas
# ============================================================================

class CreateWorkflowRequest(BaseModel):
    """Request to create a workflow (with planning)"""
    goal: str = Field(..., min_length=1, description="User's goal or question")
    tenant_id: str
    user_id: str


class ExecuteWorkflowRequest(BaseModel):
    """Request to execute a workflow"""
    workflow_id: str


# ============================================================================
# Response Schemas
# ============================================================================

class AgentPlanResponse(BaseModel):
    """Agent plan response (ephemeral chat execution plan)"""
    id: str
    tenant_id: str
    user_id: str
    goal: str
    status: str
    plan: Optional[Dict[str, Any]]  # Plan JSON
    session_id: Optional[str] = None  # Chat session ID if plan is linked to chat
    created_at: datetime
    updated_at: datetime


# Backward compatibility alias
WorkflowResponse = AgentPlanResponse


class ExecutionStepResult(BaseModel):
    """Result of a single step execution"""
    step_id: str
    status: str  # completed, failed, skipped
    result: Optional[Any] = None
    error: Optional[str] = None


class WorkflowExecutionResult(BaseModel):
    """Workflow execution result (simple version for immediate execution)"""
    workflow_id: str
    status: str  # completed, partial, failed
    completed_steps: List[str]
    failed_steps: List[str]
    step_results: Dict[str, Any]
    step_errors: Dict[str, str]


class HealthResponse(BaseModel):
    """Health check response"""
    status: str
    service: str = "meho-agent"
    version: str = "0.1.0"


# ============================================================================
# Chat Session Schemas
# ============================================================================

class CreateChatSessionRequest(BaseModel):
    """Request to create a chat session"""
    tenant_id: str
    user_id: str
    title: Optional[str] = None
    id: Optional[str] = None  # Optional client-provided ID for session continuity


class UpdateChatSessionRequest(BaseModel):
    """Request to update a chat session"""
    title: str


class ChatSessionResponse(BaseModel):
    """Chat session response"""
    id: str
    tenant_id: str
    user_id: str
    title: Optional[str]
    created_at: datetime
    updated_at: datetime
    message_count: Optional[int] = None  # Included in list view


class ChatMessageResponse(BaseModel):
    """Chat message response"""
    id: str
    role: str  # 'user' or 'assistant'
    content: str
    workflow_id: Optional[str]
    message_data: Optional[Union[Dict[str, Any], List[Dict[str, Any]]]] = None  # PydanticAI message(s)
    created_at: datetime


class ChatSessionWithMessagesResponse(BaseModel):
    """Chat session with full message history"""
    id: str
    tenant_id: str
    user_id: str
    title: Optional[str]
    created_at: datetime
    updated_at: datetime
    messages: List[ChatMessageResponse]


class AddMessageRequest(BaseModel):
    """Request to add a message to a session"""
    role: str  # 'user' or 'assistant'
    content: str
    workflow_id: Optional[str] = None
    message_data: Optional[Union[Dict[str, Any], List[Dict[str, Any]]]] = None  # PydanticAI message(s)


# ============================================================================
# Workflow Template Schemas
# ============================================================================

class CreateTemplateRequest(BaseModel):
    """Request to create a workflow template"""
    tenant_id: str
    created_by: str
    name: str
    description: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[List[str]] = None
    plan_template: Dict[str, Any]
    parameters: Optional[Dict[str, Any]] = None
    is_public: bool = False


class UpdateTemplateRequest(BaseModel):
    """Request to update a workflow template"""
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[List[str]] = None
    plan_template: Optional[Dict[str, Any]] = None
    parameters: Optional[Dict[str, Any]] = None
    is_public: Optional[bool] = None


class TemplateResponse(BaseModel):
    """Workflow template response"""
    id: str
    tenant_id: str
    created_by: str
    name: str
    description: Optional[str]
    category: Optional[str]
    tags: List[str]
    plan_template: Dict[str, Any]
    parameters: Dict[str, Any]
    is_public: bool
    execution_count: int
    last_executed_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


class ExecuteTemplateRequest(BaseModel):
    """Request to execute a workflow template"""
    parameters: Optional[Dict[str, Any]] = None
    session_id: Optional[str] = None


class ExecutionResponse(BaseModel):
    """Workflow execution response"""
    id: str
    template_id: str
    status: str
    parameters: Dict[str, Any]
    plan_json: Dict[str, Any]
    result_json: Optional[Dict[str, Any]]
    error_message: Optional[str]
    created_at: datetime
    completed_at: Optional[datetime]

