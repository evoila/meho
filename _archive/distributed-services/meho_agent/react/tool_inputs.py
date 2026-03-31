"""
Typed Input Models for ReAct Tool Nodes (TASK-92)

These Pydantic models define the expected input schema for each tool.
When ReasonNode creates a tool node, Pydantic validates the inputs
BEFORE execution - catching format errors early.

This follows pydantic-graph best practice where node fields are typed.
"""

from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, field_validator


class SortSpecInput(BaseModel):
    """Sort specification - MUST have field and direction."""
    
    field: str = Field(description="Field name to sort by")
    direction: str = Field(
        default="asc",
        description="Sort direction: 'asc' or 'desc'"
    )
    
    @field_validator('direction')
    @classmethod
    def validate_direction(cls, v: str) -> str:
        if v not in ('asc', 'desc'):
            return 'asc'  # Default to asc for invalid values
        return v


class FilterConditionInput(BaseModel):
    """A single filter condition."""
    
    field: str
    operator: str = Field(default="=")
    value: Any


class FilterGroupInput(BaseModel):
    """Group of filter conditions."""
    
    conditions: List[FilterConditionInput] = Field(default_factory=list)
    logic: str = Field(default="and")


class DataQueryInput(BaseModel):
    """
    Query specification for reduce_data tool.
    
    This is a simplified version that gets converted to the full DataQuery.
    """
    
    source_path: str = Field(default="")
    select: Optional[List[str]] = Field(default=None)
    filter: Optional[FilterGroupInput] = Field(default=None)
    sort: Optional[SortSpecInput] = Field(default=None)
    limit: Optional[int] = Field(default=None, ge=1, le=10000)
    
    @field_validator('sort', mode='before')
    @classmethod
    def normalize_sort(cls, v: Any) -> Optional[Dict[str, Any]]:
        """Normalize common LLM mistakes with sort format."""
        if v is None:
            return None
        
        # LLM might send ["name"] - convert to proper format
        if isinstance(v, list) and len(v) > 0:
            return {"field": str(v[0]), "direction": "asc"}
        
        # LLM might send {"name": "asc"} - convert to proper format
        if isinstance(v, dict) and "field" not in v:
            for key, val in v.items():
                direction = str(val) if val in ["asc", "desc"] else "asc"
                return {"field": str(key), "direction": direction}
        
        # Already proper format - ensure it's a dict
        if isinstance(v, dict):
            return dict(v)
        
        return None


class ReduceDataInput(BaseModel):
    """
    Input for reduce_data tool - query cached data using SQL.
    
    Example:
        {"sql": "SELECT * FROM virtual_machines WHERE num_cpu > 8 ORDER BY memory_mb DESC"}
    """
    
    sql: str = Field(
        description="SQL query to execute on cached tables (e.g., 'SELECT * FROM virtual_machines WHERE num_cpu > 8')"
    )


class SearchKnowledgeInput(BaseModel):
    """Input for search_knowledge tool."""
    
    query: str = Field(
        description="Natural language query for knowledge search"
    )
    limit: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Maximum number of results"
    )


class ListConnectorsInput(BaseModel):
    """Input for list_connectors tool (no required inputs)."""
    pass


# =============================================================================
# GENERIC Tool Inputs (TASK-97: Same tools for all connector types)
# =============================================================================


class SearchOperationsInput(BaseModel):
    """
    Input for search_operations tool - works for ALL connector types.
    
    TASK-97: Generic tool that routes to REST endpoints, SOAP operations,
    or VMware operations based on connector_type.
    """
    
    connector_id: str = Field(
        description="ID of the connector to search"
    )
    query: str = Field(
        min_length=1,
        description="Search query - use descriptive terms like 'disk performance', 'network metrics', 'list vms'"
    )
    limit: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Maximum number of operations to return"
    )


class CallOperationInput(BaseModel):
    """
    Input for call_operation tool - works for ALL connector types.
    
    TASK-97: Generic tool that routes to REST, SOAP, or VMware
    based on connector_type.
    
    Uses parameter_sets for ALL calls - always a list.
    Single calls have one item, batch calls have multiple items.
    """
    
    connector_id: str = Field(
        description="ID of the connector"
    )
    operation_id: str = Field(
        description="ID of the operation to call (endpoint_id for REST, operation_name for SOAP/VMware)"
    )
    parameter_sets: List[Dict[str, Any]] = Field(
        default_factory=lambda: [dict()],  # type: ignore[arg-type]
        description="List of parameter sets. Each set is executed sequentially. "
                    "For REST: each set can have 'path_params', 'query_params', 'body'. "
                    "For SOAP/VMware: each set contains the operation parameters directly."
    )


class SearchTypesInput(BaseModel):
    """
    Input for search_types tool - works for SOAP and VMware connectors.
    
    TASK-97: Generic tool that searches entity type definitions.
    """
    
    connector_id: str = Field(
        description="ID of the connector to search (required)"
    )
    query: str = Field(
        default="",
        description="Search query for type names, properties, or descriptions"
    )
    limit: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Maximum number of types to return"
    )


# Mapping from tool name to input model
TOOL_INPUT_MODELS: Dict[str, type[BaseModel]] = {
    # GENERIC TOOLS (TASK-97 - work for all connector types)
    "search_operations": SearchOperationsInput,
    "call_operation": CallOperationInput,
    "search_types": SearchTypesInput,
    
    # Other tools
    "search_knowledge": SearchKnowledgeInput,
    "list_connectors": ListConnectorsInput,
    "reduce_data": ReduceDataInput,
}


def validate_tool_input(tool_name: str, args: Dict[str, Any]) -> BaseModel:
    """
    Validate tool arguments against the appropriate input model.
    
    Args:
        tool_name: Name of the tool
        args: Raw arguments from LLM
        
    Returns:
        Validated Pydantic model
        
    Raises:
        ValueError: If tool_name is unknown
        ValidationError: If args don't match the model
    """
    model_class = TOOL_INPUT_MODELS.get(tool_name)
    if not model_class:
        raise ValueError(f"Unknown tool: {tool_name}")
    
    return model_class(**args)

