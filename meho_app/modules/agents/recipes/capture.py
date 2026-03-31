# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Recipe Capture Service.

Captures Q&A interactions and extracts reusable recipes from them.
The key challenge is identifying which parts of the question/query
are "parameters" that users might want to change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic_ai import Agent

from meho_app.core.otel import get_logger
from meho_app.modules.agents.data_reduction.query_schema import (
    DataQuery,
    FilterCondition,
    FilterGroup,
)
from meho_app.modules.agents.recipes.models import (
    Recipe,
    RecipeParameter,
    RecipeParameterType,
    RecipeQueryTemplate,
)

logger = get_logger(__name__)


@dataclass
class CaptureContext:
    """Context for recipe capture."""

    question: str
    query: DataQuery
    connector_id: UUID
    endpoint_id: UUID | None = None
    response_schema: dict[str, Any] | None = None


class RecipeCaptureService:
    """
    Service for capturing Q&A interactions as recipes.

    The capture process:
    1. Analyze the original question for variable parts
    2. Analyze the generated query for parameterizable values
    3. Match question patterns to query values
    4. Generate parameter definitions
    5. Create a recipe template

    Example:
        Input:
            question: "Show clusters in us-east with memory > 80%"
            query: DataQuery(
                filter=[region == "us-east", memory_pct > 80],
                ...
            )

        Output:
            Recipe with parameters:
            - region: string, default="us-east"
            - memory_threshold: number, default=80
    """

    def __init__(self) -> None:
        """Initialize the capture service."""
        self._parameter_agent: Agent | None = None

    async def capture(
        self,
        question: str,
        query: DataQuery,
        connector_id: UUID,
        tenant_id: str,
        endpoint_id: UUID | None = None,
        name: str | None = None,
        description: str | None = None,
    ) -> Recipe:
        """
        Capture a Q&A interaction as a recipe.

        Args:
            question: The original natural language question
            query: The generated DataQuery
            connector_id: Which connector was used
            tenant_id: Tenant context
            endpoint_id: Optional specific endpoint
            name: Optional recipe name (auto-generated if not provided)
            description: Optional description

        Returns:
            A Recipe ready to be saved
        """
        # Extract parameters from question and query
        parameters = self._extract_parameters(question, query)

        # Create the query template
        template = self._create_template(query, parameters)

        # Generate name if not provided
        if not name:
            name = self._generate_name(question, parameters)

        # Generate description if not provided
        if not description:
            description = self._generate_description(question, parameters)

        return Recipe(
            tenant_id=tenant_id,
            name=name,
            description=description,
            connector_id=connector_id,
            endpoint_id=endpoint_id,
            original_question=question,
            parameters=parameters,
            query_template=template,
            tags=self._extract_tags(question, query),
        )

    def _extract_parameters(
        self,
        question: str,
        query: DataQuery,
    ) -> list[RecipeParameter]:
        """
        Extract parameters from the question and query.

        This uses heuristics to identify values that users might
        want to change when re-running the recipe.
        """
        parameters: list[RecipeParameter] = []
        seen_names: set[str] = set()

        # Extract from filter conditions
        if query.filter:
            params = self._extract_filter_parameters(query.filter, question, seen_names)
            parameters.extend(params)

        # Extract from compute expressions
        for compute in query.compute:
            # Look for threshold values in expressions
            params = self._extract_compute_parameters(
                compute.name, compute.expression, question, seen_names
            )
            parameters.extend(params)

        # Extract from limit
        if query.limit and query.limit not in [10, 20, 50, 100]:  # noqa: SIM102 -- readability preferred over collapse
            # Non-standard limit might be a parameter
            if "limit" not in seen_names:
                parameters.append(
                    RecipeParameter(
                        name="limit",
                        display_name="Result Limit",
                        description="Maximum number of results to return",
                        param_type=RecipeParameterType.INTEGER,
                        default_value=query.limit,
                        required=False,
                        min_value=1,
                        max_value=1000,
                    )
                )
                seen_names.add("limit")

        return parameters

    def _extract_filter_parameters(
        self,
        filter_group: FilterGroup,
        question: str,
        seen_names: set[str],
    ) -> list[RecipeParameter]:
        """Extract parameters from filter conditions."""
        parameters = []

        for condition in filter_group.conditions:
            if isinstance(condition, FilterGroup):
                # Recurse into nested groups
                params = self._extract_filter_parameters(condition, question, seen_names)
                parameters.extend(params)
            elif isinstance(condition, FilterCondition):
                param = self._condition_to_parameter(condition, question, seen_names)
                if param:
                    parameters.append(param)
                    seen_names.add(param.name)

        return parameters

    def _condition_to_parameter(
        self,
        condition: FilterCondition,
        question: str,
        seen_names: set[str],
    ) -> RecipeParameter | None:
        """Convert a filter condition to a parameter."""
        field = condition.field
        value = condition.value
        operator = condition.operator

        # Generate a parameter name from the field
        base_name = field.split(".")[-1].replace("_", " ").title()
        param_name = self._sanitize_param_name(field)

        # Skip if already seen
        if param_name in seen_names:
            return None

        # Determine type based on value
        if isinstance(value, bool):
            param_type = RecipeParameterType.BOOLEAN
        elif isinstance(value, int):
            param_type = RecipeParameterType.INTEGER
        elif isinstance(value, float):
            param_type = RecipeParameterType.NUMBER
        elif isinstance(value, list):
            param_type = RecipeParameterType.ARRAY
        else:
            param_type = RecipeParameterType.STRING

        # Generate display name
        if ">" in str(operator) or "<" in str(operator):
            display_name = f"{base_name} Threshold"
        elif "in" in str(operator).lower():
            display_name = f"{base_name} Values"
        else:
            display_name = base_name

        # Generate description based on operator
        if operator in [">", ">=", "gt", "gte"]:
            description = f"Minimum {base_name.lower()} value"
        elif operator in ["<", "<=", "lt", "lte"]:
            description = f"Maximum {base_name.lower()} value"
        elif operator == "contains":
            description = f"Text to search for in {base_name.lower()}"
        else:
            description = f"Value to filter by {base_name.lower()}"

        return RecipeParameter(
            name=param_name,
            display_name=display_name,
            description=description,
            param_type=param_type,
            default_value=value,
            required=True,
            source_field=field,
            source_expression=f"{field} {operator} {value}",
        )

    def _extract_compute_parameters(
        self,
        name: str,
        expression: str,
        question: str,
        seen_names: set[str],
    ) -> list[RecipeParameter]:
        """Extract parameters from compute expressions."""
        parameters = []

        # Look for numeric constants in the expression
        numbers = re.findall(r"\b(\d+(?:\.\d+)?)\b", expression)
        for num in numbers:
            # Skip common constants like 100 (for percentages)
            if num in ["100", "1", "0"]:
                continue

            param_name = f"{name}_threshold"
            if param_name in seen_names:
                continue

            parameters.append(
                RecipeParameter(
                    name=param_name,
                    display_name=f"{name.replace('_', ' ').title()} Threshold",
                    description=f"Threshold value for {name.replace('_', ' ')}",
                    param_type=RecipeParameterType.NUMBER,
                    default_value=float(num),
                    required=False,
                    source_expression=expression,
                )
            )
            seen_names.add(param_name)
            break  # Only extract one parameter per expression

        return parameters

    def _create_template(
        self,
        query: DataQuery,
        parameters: list[RecipeParameter],
    ) -> RecipeQueryTemplate:
        """Create a query template from the query and parameters."""
        # Map parameters to their source fields
        param_by_field = {p.source_field: p for p in parameters if p.source_field}

        # Create filter template with placeholders
        filter_template = None
        if query.filter:
            filter_template = self._create_filter_template(query.filter, param_by_field)

        # Create compute expressions
        compute_expressions = []
        for comp in query.compute:
            compute_expressions.append(
                {
                    "name": comp.name,
                    "expression": comp.expression,
                }
            )

        # Create aggregate list
        aggregates = []
        for agg in query.aggregates:
            aggregates.append(
                {
                    "name": agg.name,
                    "function": agg.function,
                    "field": agg.field,
                }
            )

        return RecipeQueryTemplate(
            source_path=query.source_path,
            select=query.select,
            compute_expressions=compute_expressions,
            filter_template=filter_template,
            sort_field=query.sort.field if query.sort else None,
            sort_direction=query.sort.direction if query.sort else None,
            limit=query.limit,
            aggregates=aggregates,
        )

    def _create_filter_template(
        self,
        filter_group: FilterGroup,
        param_by_field: dict[str, RecipeParameter],
    ) -> dict[str, Any]:
        """Create filter template with parameter placeholders."""
        conditions = []

        for condition in filter_group.conditions:
            if isinstance(condition, FilterGroup):
                # Nested group
                conditions.append(self._create_filter_template(condition, param_by_field))
            elif isinstance(condition, FilterCondition):
                field = condition.field

                # Check if this field has a parameter
                if field in param_by_field:
                    param = param_by_field[field]
                    value: Any = f"{{{{{param.name}}}}}"  # Template placeholder
                else:
                    value = condition.value

                conditions.append(
                    {
                        "field": field,
                        "operator": str(condition.operator),
                        "value": value,
                    }
                )

        return {
            "conditions": conditions,
            "logic": filter_group.logic,
        }

    def _generate_name(
        self,
        question: str,
        parameters: list[RecipeParameter],
    ) -> str:
        """Generate a recipe name from the question."""
        # Extract key phrases
        name = question

        # Remove common prefixes
        for prefix in ["show me", "list", "find", "get", "what are", "which"]:
            if name.lower().startswith(prefix):
                name = name[len(prefix) :].strip()

        # Capitalize and clean up
        name = name.strip().capitalize()

        # Truncate if too long
        if len(name) > 50:
            name = name[:47] + "..."

        return name or "Unnamed Recipe"

    def _generate_description(
        self,
        question: str,
        parameters: list[RecipeParameter],
    ) -> str:
        """Generate a recipe description."""
        if not parameters:
            return f"Runs the query: {question}"

        param_names = [p.display_name for p in parameters]
        return f"Runs the query: {question}\n\nParameters: {', '.join(param_names)}"

    def _extract_tags(
        self,
        question: str,
        query: DataQuery,
    ) -> list[str]:
        """Extract tags from the question and query."""
        tags = []

        # Add tags based on aggregates
        if query.aggregates:
            tags.append("analytics")

        # Add tags based on filter operators
        if query.filter:
            has_threshold = any(
                isinstance(c, FilterCondition) and str(c.operator) in [">", "<", ">=", "<="]
                for c in query.filter.conditions
            )
            if has_threshold:
                tags.append("threshold-based")

        # Add source path as tag
        if query.source_path:
            tags.append(query.source_path.split(".")[0])

        return tags

    def _sanitize_param_name(self, field: str) -> str:
        """Convert a field name to a valid parameter name."""
        # Remove special characters
        name = re.sub(r"[^a-zA-Z0-9_]", "_", field)
        # Remove consecutive underscores
        name = re.sub(r"_+", "_", name)
        # Remove leading/trailing underscores
        name = name.strip("_")
        return name.lower()
