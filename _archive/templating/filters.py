"""Custom Jinja2 filters for data operations.

These filters enable powerful data transformations in templates:
- sum_attr: Sum numeric attribute across items
- avg_attr: Average numeric attribute
- selectattr_custom: Filter items by attribute conditions
- groupby_attr: Group items by attribute value
- json_dumps: Convert to JSON (for LLM prompts)
"""

import json
from typing import Any, Callable, Dict, List, Optional

from jinja2 import Environment


def get_nested_attr(obj: Any, path: str) -> Any:
    """Get nested attribute using dot notation.
    
    Args:
        obj: Object to traverse
        path: Dot-separated path (e.g., "capacity.cpu.total.value")
        
    Returns:
        Value at the path, or None if not found
        
    Examples:
        >>> obj = {"capacity": {"cpu": {"total": {"value": 42}}}}
        >>> get_nested_attr(obj, "capacity.cpu.total.value")
        42
    """
    parts = path.split(".")
    current = obj
    
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        elif hasattr(current, part):
            current = getattr(current, part)
        else:
            return None
            
        if current is None:
            return None
            
    return current


def sum_attr(items: List[Any], attr: str, default: float = 0.0) -> float:
    """Sum numeric attribute across items.
    
    Usage:
        {{ clusters | sum_attr('capacity.cpu.total.value') }}
        
    Args:
        items: List of objects
        attr: Dot-separated attribute path
        default: Value to use if attribute is missing/non-numeric
        
    Returns:
        Sum of attribute values
    """
    if not items:
        return 0.0
        
    total = 0.0
    for item in items:
        value = get_nested_attr(item, attr)
        if value is not None:
            try:
                total += float(value)
            except (TypeError, ValueError):
                total += default
                
    return total


def avg_attr(items: List[Any], attr: str, default: float = 0.0) -> float:
    """Average numeric attribute across items.
    
    Usage:
        {{ clusters | avg_attr('utilization.cpu') }}
        
    Args:
        items: List of objects
        attr: Dot-separated attribute path
        default: Value to use if attribute is missing/non-numeric
        
    Returns:
        Average of attribute values
    """
    if not items:
        return 0.0
        
    total = sum_attr(items, attr, default=default)
    return total / len(items)


def selectattr_custom(
    items: List[Any],
    attr: str,
    op: str,
    value: Any,
) -> List[Any]:
    """Filter items by attribute condition.
    
    Usage:
        {{ clusters | selectattr_custom('status', 'eq', 'ACTIVE') }}
        {{ clusters | selectattr_custom('utilization', 'gt', 80) }}
        
    Args:
        items: List of objects
        attr: Dot-separated attribute path
        op: Operator ('eq', 'ne', 'lt', 'le', 'gt', 'ge', 'in', 'contains')
        value: Value to compare against
        
    Returns:
        Filtered list of items
    """
    if not items:
        return []
        
    operators: Dict[str, Callable[[Any, Any], bool]] = {
        "eq": lambda a, b: a == b,
        "ne": lambda a, b: a != b,
        "lt": lambda a, b: a < b,
        "le": lambda a, b: a <= b,
        "gt": lambda a, b: a > b,
        "ge": lambda a, b: a >= b,
        "in": lambda a, b: a in b,
        "contains": lambda a, b: b in a,
    }
    
    if op not in operators:
        raise ValueError(f"Unknown operator: {op}")
        
    op_func = operators[op]
    result = []
    
    for item in items:
        item_value = get_nested_attr(item, attr)
        if item_value is not None:
            try:
                if op_func(item_value, value):
                    result.append(item)
            except (TypeError, ValueError):
                # Skip items where comparison fails
                pass
                
    return result


def groupby_attr(items: List[Any], attr: str) -> Dict[Any, List[Any]]:
    """Group items by attribute value.
    
    Usage:
        {{ clusters | groupby_attr('domain.id') }}
        
    Args:
        items: List of objects
        attr: Dot-separated attribute path
        
    Returns:
        Dict mapping attribute values to lists of items
    """
    if not items:
        return {}
        
    groups: Dict[Any, List[Any]] = {}
    
    for item in items:
        key = get_nested_attr(item, attr)
        if key is not None:
            if key not in groups:
                groups[key] = []
            groups[key].append(item)
            
    return groups


def json_dumps(value: Any, indent: Optional[int] = 2) -> str:
    """Convert value to JSON string.
    
    Useful for including data in LLM prompts.
    
    Usage:
        {{ clusters | json_dumps }}
        {{ data | json_dumps(indent=None) }}  # Compact
        
    Args:
        value: Value to serialize
        indent: JSON indentation (None for compact)
        
    Returns:
        JSON string
    """
    return json.dumps(value, indent=indent, default=str)


def register_custom_filters(env: Environment) -> None:
    """Register all custom filters with Jinja2 environment.
    
    Args:
        env: Jinja2 environment
    """
    env.filters["sum_attr"] = sum_attr
    env.filters["avg_attr"] = avg_attr
    env.filters["selectattr_custom"] = selectattr_custom
    env.filters["groupby_attr"] = groupby_attr
    env.filters["json_dumps"] = json_dumps
    
    # Also register shorter aliases
    env.filters["sum"] = sum_attr
    env.filters["avg"] = avg_attr
    env.filters["selectattr"] = selectattr_custom
    env.filters["groupby"] = groupby_attr
    env.filters["json"] = json_dumps

