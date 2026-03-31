"""
Data shape inference for conversational workflow building.

Analyzes API responses to discover available data paths for LLM suggestions.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Set


@dataclass
class BuilderStepInfo:
    """Information about a step during conversational building."""
    
    id: str
    action: str
    output: Any
    output_schema: Dict[str, Any]
    available_paths: List[str]


class BuilderContext:
    """
    Context for conversational workflow building.
    
    Tracks executed steps and their outputs to help LLM suggest
    data transformations based on actual data shapes.
    """
    
    def __init__(self) -> None:
        self.steps: Dict[str, BuilderStepInfo] = {}
    
    def add_step(
        self,
        step_id: str,
        action: str,
        output: Any
    ) -> BuilderStepInfo:
        """
        Add a step and infer its data shape.
        
        Args:
            step_id: Step identifier
            action: Action type (call_endpoint, transform, etc.)
            output: Step output data
            
        Returns:
            BuilderStepInfo with inferred schema and paths
        """
        # Infer schema and available paths
        schema = infer_schema(output)
        paths = extract_paths(output, prefix="output")
        
        step_info = BuilderStepInfo(
            id=step_id,
            action=action,
            output=output,
            output_schema=schema,
            available_paths=paths
        )
        
        self.steps[step_id] = step_info
        return step_info
    
    def get_step(self, step_id: str) -> BuilderStepInfo:
        """Get info for a specific step."""
        return self.steps[step_id]
    
    def get_all_paths(self) -> Dict[str, List[str]]:
        """
        Get all available paths for all steps.
        
        Returns:
            Dict mapping step_id to list of available paths
        """
        return {
            step_id: info.available_paths
            for step_id, info in self.steps.items()
        }
    
    def format_for_llm(self) -> str:
        """
        Format context for LLM prompt.
        
        Returns formatted string showing available data paths
        for LLM to use in generating Jinja2 expressions.
        """
        if not self.steps:
            return "No steps executed yet."
        
        lines = ["Available data from previous steps:"]
        lines.append("")
        
        for step_id, info in self.steps.items():
            lines.append(f"Step: {step_id} ({info.action})")
            lines.append(f"  Access as: steps.{step_id}.output")
            lines.append("  Available paths:")
            
            # Show top 10 paths (to avoid overwhelming the LLM)
            for path in info.available_paths[:10]:
                lines.append(f"    - {path}")
            
            if len(info.available_paths) > 10:
                lines.append(f"    ... and {len(info.available_paths) - 10} more")
            
            lines.append("")
        
        return "\n".join(lines)


def infer_schema(data: Any, max_depth: int = 5) -> Dict[str, Any]:
    """
    Infer JSON schema from data.
    
    Args:
        data: Data to analyze
        max_depth: Maximum nesting depth to analyze
        
    Returns:
        JSON Schema-like dict describing the data structure
    """
    if max_depth <= 0:
        return {"type": "unknown"}
    
    if data is None:
        return {"type": "null"}
    
    if isinstance(data, bool):
        return {"type": "boolean"}
    
    if isinstance(data, int):
        return {"type": "integer"}
    
    if isinstance(data, float):
        return {"type": "number"}
    
    if isinstance(data, str):
        return {"type": "string"}
    
    if isinstance(data, list):
        if not data:
            return {"type": "array", "items": {}}
        
        # Infer schema from first item (assuming homogeneous array)
        item_schema = infer_schema(data[0], max_depth - 1)
        return {
            "type": "array",
            "items": item_schema,
            "length": len(data)
        }
    
    if isinstance(data, dict):
        properties = {}
        for key, value in data.items():
            properties[key] = infer_schema(value, max_depth - 1)
        
        return {
            "type": "object",
            "properties": properties
        }
    
    return {"type": "unknown"}


def extract_paths(
    data: Any,
    prefix: str = "",
    max_depth: int = 5,
    max_paths: int = 100
) -> List[str]:
    """
    Extract all possible dot-notation paths from data.
    
    Args:
        data: Data to analyze
        prefix: Current path prefix
        max_depth: Maximum nesting depth
        max_paths: Maximum number of paths to return
        
    Returns:
        List of dot-notation paths (e.g., "output.elements[].name")
        
    Examples:
        >>> data = {"elements": [{"name": "x", "value": 1}]}
        >>> extract_paths(data, "output")
        ['output', 'output.elements', 'output.elements[].name', 'output.elements[].value']
    """
    paths: Set[str] = set()
    
    def _extract(obj: Any, path: str, depth: int) -> None:
        if depth <= 0 or len(paths) >= max_paths:
            return
        
        # Add current path
        if path:
            paths.add(path)
        
        if isinstance(obj, dict):
            for key, value in obj.items():
                new_path = f"{path}.{key}" if path else key
                _extract(value, new_path, depth - 1)
        
        elif isinstance(obj, list) and obj:
            # Add array indicator
            array_path = f"{path}[]"
            paths.add(array_path)
            
            # Analyze first item (assuming homogeneous arrays)
            if isinstance(obj[0], dict):
                for key in obj[0].keys():
                    field_path = f"{array_path}.{key}"
                    paths.add(field_path)
                    
                    # Recurse one level for nested objects
                    if depth > 1 and isinstance(obj[0][key], (dict, list)):
                        _extract(obj[0][key], field_path, depth - 1)
    
    _extract(data, prefix, max_depth)
    
    # Sort paths for consistent ordering
    return sorted(paths)


def suggest_jinja_expressions(
    context: BuilderContext,
    user_intent: str
) -> List[str]:
    """
    Suggest Jinja2 expressions based on user intent and available data.
    
    Args:
        context: Builder context with step outputs
        user_intent: What the user wants to do (e.g., "sum the CPU")
        
    Returns:
        List of suggested Jinja2 expressions
        
    Examples:
        >>> context.add_step("get_clusters", "call_endpoint", {
        ...     "elements": [{"name": "c1", "cpu": 100}, {"name": "c2", "cpu": 200}]
        ... })
        >>> suggest_jinja_expressions(context, "sum cpu")
        ['{{ steps.get_clusters.output.elements | sum(\'cpu\') }}']
    """
    suggestions = []
    intent_lower = user_intent.lower()
    
    # Simple keyword matching for common operations
    # (In production, this would use an LLM)
    
    for step_id, info in context.steps.items():
        paths = info.available_paths
        
        # Detect aggregation intents
        if any(word in intent_lower for word in ["sum", "total", "add"]):
            # Look for numeric fields
            for path in paths:
                if "[]." in path:  # Array element field
                    field = path.split("[].")[-1]
                    base_path = path.split("[].")[0]
                    suggestions.append(
                        f"{{{{ steps.{step_id}.{base_path} | sum('{field}') }}}}"
                    )
        
        if any(word in intent_lower for word in ["average", "avg", "mean"]):
            for path in paths:
                if "[]." in path:
                    field = path.split("[].")[-1]
                    base_path = path.split("[].")[0]
                    suggestions.append(
                        f"{{{{ steps.{step_id}.{base_path} | avg('{field}') }}}}"
                    )
        
        if any(word in intent_lower for word in ["count", "how many", "number of"]):
            for path in paths:
                if path.endswith("[]"):
                    suggestions.append(
                        f"{{{{ steps.{step_id}.{path[:-2]} | length }}}}"
                    )
        
        if any(word in intent_lower for word in ["filter", "where", "only"]):
            for path in paths:
                if "[]." in path:
                    field = path.split("[].")[-1]
                    base_path = path.split("[].")[0]
                    suggestions.append(
                        f"{{{{ steps.{step_id}.{base_path} | selectattr('{field}', 'eq', 'VALUE') }}}}"
                    )
    
    return suggestions[:5]  # Return top 5 suggestions

