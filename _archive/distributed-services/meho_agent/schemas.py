"""
Pydantic schemas for agent service.

Note: WorkflowDefinition* schemas removed in Session 80.
Replaced by Recipe system (meho_agent/recipes/).
"""
# mypy: disable-error-code="no-untyped-def,var-annotated"
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import List, Dict, Any, Optional, ClassVar
import uuid
from datetime import datetime


class PlanStep(BaseModel):
    """A single step in a plan"""
    REQUIRED_TOOL_ARGS: ClassVar[Dict[str, List[str]]] = {
        "get_endpoint_details": ["connector_id", "search_query"],
        "search_endpoints": ["connector_id", "query"],  # NEW Session 60
        "determine_connector": ["query"],               # NEW Session 60
        "call_endpoint": ["connector_id", "endpoint_id"],
        # Note: search_knowledge validated separately (needs queries OR query)
    }

    id: str
    description: str
    tool_name: str
    tool_args: Dict[str, Any] = Field(default_factory=dict)  # Default to empty dict if LLM doesn't provide
    depends_on: List[str] = Field(default_factory=list)
    
    @field_validator('id')
    @classmethod
    def validate_id(cls, v):
        if not v or not v.strip():
            raise ValueError("Step ID cannot be empty")
        # Strip and return normalized ID to prevent whitespace issues
        stripped = v.strip()
        if stripped != v:
            raise ValueError("Step ID cannot have leading or trailing whitespace")
        return v

    @model_validator(mode="after")
    def validate_tool_args(self) -> "PlanStep":
        # Validate search_knowledge first (special case - needs queries OR query)
        if self.tool_name == "search_knowledge":
            queries = self.tool_args.get("queries")
            single_query = self.tool_args.get("query")

            if queries is None and single_query is None:
                # Provide helpful error with the actual tool_args content
                raise ValueError(
                    f"Tool 'search_knowledge' requires 'queries' (preferred) or 'query' argument. "
                    f"Received tool_args: {self.tool_args}. "
                    f"Example: {{'queries': ['VCF clusters', 'cluster API']}} or {{'query': 'VCF clusters'}}"
                )

            if queries is not None:
                if isinstance(queries, str):
                    if not queries.strip():
                        raise ValueError("search_knowledge.queries cannot be empty")
                elif isinstance(queries, list):
                    if not queries or not all(isinstance(q, str) and q.strip() for q in queries):
                        raise ValueError("search_knowledge.queries must be a list of non-empty strings")
                else:
                    raise ValueError("search_knowledge.queries must be a string or list of strings")
            if single_query is not None and isinstance(single_query, str) and not single_query.strip():
                raise ValueError("search_knowledge.query cannot be empty")
        
        # Validate other tools with REQUIRED_TOOL_ARGS
        required_args = self.REQUIRED_TOOL_ARGS.get(self.tool_name)
        if required_args:
            missing = []
            for arg in required_args:
                if arg not in self.tool_args:
                    missing.append(arg)
                    continue
                value = self.tool_args[arg]
                if value is None:
                    missing.append(arg)
                    continue
                if isinstance(value, str) and not value.strip():
                    missing.append(arg)
                    continue
                if arg in {"connector_id", "endpoint_id"}:
                    # Allow placeholder values for dependent steps (e.g., "<from-step1>")
                    # These will be resolved at execution time from previous step results
                    if isinstance(value, str) and value.startswith("<") and value.endswith(">"):
                        continue  # Placeholder is valid for dependent steps
                    try:
                        uuid.UUID(str(value))
                    except (ValueError, TypeError):
                        missing.append(arg)
            if missing:
                raise ValueError(
                    f"Tool '{self.tool_name}' requires arguments: {', '.join(required_args)} "
                    f"(missing: {', '.join(missing)})"
                )
        
        return self


class Plan(BaseModel):
    """Complete execution plan"""
    # Tools that retrieve data/context (for validation)
    RETRIEVAL_TOOLS: ClassVar[set[str]] = {
        "search_apis",       # Search OpenAPI specs for endpoints
        "search_docs",       # Search documentation for concepts
        "search_knowledge",  # Search knowledge base
        "list_connectors",   # List available connectors
        "search_operations", # Search operations (generic - all connector types)
        "call_operation",    # Execute operations (generic - all connector types)
    }
    
    goal: str
    steps: List[PlanStep]
    notes: Optional[str] = None
    
    @field_validator('steps')
    @classmethod
    def validate_steps(cls, v):
        # Require at least one step - LLM must propose specific actions
        if not v:
            raise ValueError("Plan must have at least one step")
        
        # Check for duplicate IDs
        step_ids = [s.id for s in v]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("Step IDs must be unique")
        
        # Require the first step to be a search or connector determination
        # Session 60: Added determine_connector to valid first steps for new planning flow
        valid_first_steps = {"search_apis", "search_docs", "search_knowledge", "determine_connector"}
        search_tools = {"search_apis", "search_docs", "search_knowledge"}
        
        if v[0].tool_name not in valid_first_steps:
            raise ValueError("First plan step must be a search tool (search_apis, search_docs) or determine_connector to establish context")

        # Plan should have at least one search OR connector determination step
        has_search_or_connector = any(step.tool_name in valid_first_steps for step in v)
        if not has_search_or_connector:
            raise ValueError("Plan must include at least one search step (search_apis, search_docs) or determine_connector")

        interpret_steps = [step for step in v if step.tool_name == "interpret_results"]
        if not interpret_steps:
            raise ValueError("Plan must include an interpret_results step to synthesize findings for the user")

        if v[-1].tool_name != "interpret_results":
            raise ValueError("Final plan step must be interpret_results so the executor can provide an answer")

        # Validate dependencies reference existing steps
        for step in v:
            for dep in step.depends_on:
                if dep not in step_ids:
                    raise ValueError(f"Step {step.id} depends on non-existent step {dep}")
        
        # Detect circular dependencies using DFS
        def has_cycle(step_id: str, visited: set, rec_stack: set, dep_map: dict) -> bool:
            visited.add(step_id)
            rec_stack.add(step_id)
            
            for dep in dep_map.get(step_id, []):
                if dep not in visited:
                    if has_cycle(dep, visited, rec_stack, dep_map):
                        return True
                elif dep in rec_stack:
                    return True
            
            rec_stack.remove(step_id)
            return False
        
        # Build dependency map
        dep_map = {s.id: s.depends_on for s in v}
        step_map = {s.id: s for s in v}
        visited = set()
        
        for step in v:
            if step.id not in visited:
                if has_cycle(step.id, visited, set(), dep_map):
                    raise ValueError(f"Circular dependency detected involving step {step.id}")

        # Ensure interpret_results steps depend on at least one prior retrieval step
        for step in v:
            if step.tool_name == "interpret_results":
                if not step.depends_on:
                    raise ValueError("interpret_results steps must depend on at least one prior step")
                if not any(
                    step_map.get(dep) and step_map[dep].tool_name in cls.RETRIEVAL_TOOLS
                    for dep in step.depends_on
                ):
                    raise ValueError(
                        "interpret_results steps must depend on a retrieval/tool step (search_knowledge, list_connectors, get_endpoint_details, call_endpoint)"
                    )
        
        return v


class AgentPlanCreate(BaseModel):
    """Create an agent plan (ephemeral chat execution)"""
    tenant_id: str
    user_id: str
    goal: str
    plan: Optional[Plan] = None


class AgentPlan(BaseModel):
    """Agent plan with ID and status (ephemeral - for chat responses)"""
    id: str
    tenant_id: str
    user_id: str
    status: str
    goal: str
    plan_json: Optional[Dict[str, Any]]
    current_step_index: int
    session_id: Optional[str] = None  # Chat session ID if plan is linked to chat
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class WorkflowStep(BaseModel):
    """Workflow step execution details"""
    id: str
    workflow_id: str
    index: int
    tool_name: str
    input_json: Dict[str, Any]
    output_json: Optional[Dict[str, Any]]
    status: str
    error_message: Optional[str]
    started_at: Optional[datetime]
    finished_at: Optional[datetime]


class WorkflowExecutionStepResult(BaseModel):
    """Result of executing a single step in a workflow/recipe."""
    status: str = Field(..., description="Step status: PENDING, COMPLETED, FAILED")
    output: Optional[Dict[str, Any]] = Field(None, description="Step output data")
    error: Optional[str] = Field(None, description="Error message if failed")
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


# =============================================================================
# DEPRECATED: WorkflowDefinition* schemas removed in Session 80
# Replaced by Recipe system (meho_agent/recipes/)
# =============================================================================


# Backward compatibility aliases (to be removed after full migration)
# These allow existing code to work while we transition to clearer names
Workflow = AgentPlan
WorkflowCreate = AgentPlanCreate
