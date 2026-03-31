# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Recipe Data Models.

These models define the structure of saved recipes -
reusable Q&A patterns that users can execute with different parameters.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from meho_app.modules.agents.data_reduction.query_schema import DataQuery


class RecipeParameterType(StrEnum):
    """Types of parameters that can be extracted from queries."""

    STRING = "string"  # Text values (e.g., region names, statuses)
    NUMBER = "number"  # Numeric values (e.g., thresholds, counts)
    INTEGER = "integer"  # Integer values
    BOOLEAN = "boolean"  # True/False flags
    ENUM = "enum"  # Predefined choices
    DATE = "date"  # Date values
    DATETIME = "datetime"  # Date and time values
    ARRAY = "array"  # Lists of values


class RecipeParameter(BaseModel):
    """
    A user-configurable parameter in a recipe.

    Parameters are extracted from the original question and query,
    allowing users to customize the recipe execution.

    Example:
        Original: "Show clusters in us-east with memory > 80%"
        Parameters:
        - name="region", type=STRING, default="us-east"
        - name="memory_threshold", type=NUMBER, default=80
    """

    model_config = ConfigDict(use_enum_values=True)

    name: str = Field(description="Parameter name (used in template substitution)")
    display_name: str = Field(description="Human-readable name for UI")
    description: str | None = Field(default=None, description="Help text explaining the parameter")
    param_type: RecipeParameterType = Field(description="Type of the parameter")
    default_value: Any | None = Field(default=None, description="Default value for the parameter")
    required: bool = Field(default=True, description="Whether the parameter is required")

    # For enum type
    allowed_values: list[Any] | None = Field(
        default=None, description="Allowed values for enum type"
    )

    # For number types
    min_value: float | None = Field(default=None, description="Minimum allowed value")
    max_value: float | None = Field(default=None, description="Maximum allowed value")

    # Source tracking
    source_field: str | None = Field(
        default=None, description="The query field this parameter maps to"
    )
    source_expression: str | None = Field(
        default=None, description="The original expression (e.g., 'memory_pct > 80')"
    )


class RecipeQueryTemplate(BaseModel):
    """
    Template for generating a DataQuery with parameter substitution.

    The template contains placeholders like {{region}} that get
    replaced with actual values when the recipe is executed.
    """

    # Base query structure (with placeholders)
    source_path: str = Field(description="Path to extract data from response")

    # Field selection (may contain parameter references)
    select: list[str] | None = Field(default=None, description="Fields to select")

    # Computed fields (expressions may reference parameters)
    compute_expressions: list[dict[str, str]] = Field(
        default_factory=list, description="Computed field definitions"
    )

    # Filter template (conditions reference parameters)
    filter_template: dict[str, Any] | None = Field(
        default=None, description="Filter structure with parameter placeholders"
    )

    # Sort specification
    sort_field: str | None = Field(default=None)
    sort_direction: str | None = Field(default="desc")

    # Pagination
    limit: int | None = Field(default=None)

    # Aggregations
    aggregates: list[dict[str, Any]] = Field(
        default_factory=list, description="Aggregate specifications"
    )

    def render(self, parameters: dict[str, Any]) -> DataQuery:
        """
        Render the template with actual parameter values.

        Args:
            parameters: Parameter name to value mapping

        Returns:
            DataQuery ready for execution
        """
        from meho_app.modules.agents.data_reduction.query_schema import (
            AggregateSpec,
            ComputeField,
            FilterCondition,
            FilterGroup,
            SortSpec,
        )

        # Simple placeholder substitution
        def substitute(value: Any) -> Any:
            if isinstance(value, str):
                for name, param_value in parameters.items():
                    placeholder = f"{{{{{name}}}}}"
                    if placeholder in value:
                        if value == placeholder:
                            return param_value
                        value = value.replace(placeholder, str(param_value))
                return value
            elif isinstance(value, dict):
                return {k: substitute(v) for k, v in value.items()}
            elif isinstance(value, list):
                return [substitute(v) for v in value]
            return value

        # Build the query
        computed = []
        for comp in self.compute_expressions:
            computed.append(
                ComputeField(name=comp["name"], expression=substitute(comp["expression"]))
            )

        # Build filter
        filter_group = None
        if self.filter_template:
            template = substitute(self.filter_template)
            conditions = []
            for cond in template.get("conditions", []):
                conditions.append(
                    FilterCondition(
                        field=cond["field"], operator=cond["operator"], value=cond["value"]
                    )
                )
            if conditions:
                filter_group = FilterGroup(
                    conditions=conditions,  # type: ignore[arg-type]
                    logic=template.get("logic", "and"),
                )

        # Build aggregates
        aggs = []
        for agg in self.aggregates:
            aggs.append(
                AggregateSpec(name=agg["name"], function=agg["function"], field=agg["field"])
            )

        # Build sort
        sort_spec = None
        if self.sort_field:
            direction = self.sort_direction or "desc"
            sort_spec = SortSpec(
                field=substitute(self.sort_field),
                direction=direction if direction in ("asc", "desc") else "desc",  # type: ignore[arg-type]
            )

        return DataQuery(
            source_path=self.source_path,
            select=substitute(self.select) if self.select else None,
            compute=computed,
            filter=filter_group,
            sort=sort_spec,
            limit=self.limit,
            aggregates=aggs,
        )


class Recipe(BaseModel):
    """
    A saved recipe - a reusable Q&A pattern.

    Recipes capture successful Q&A interactions and allow users
    to replay them with different parameter values.
    """

    model_config = ConfigDict(use_enum_values=True)

    # Identity
    id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(description="Tenant that owns this recipe")

    # Metadata
    name: str = Field(description="Recipe name")
    description: str | None = Field(default=None, description="What this recipe does")
    tags: list[str] = Field(default_factory=list, description="Tags for categorization")

    # Source information
    connector_id: UUID = Field(description="Which connector this recipe uses")
    endpoint_id: UUID | None = Field(default=None, description="Specific endpoint if applicable")

    # The original question that created this recipe
    original_question: str = Field(
        description="The natural language question that spawned this recipe"
    )

    # Parameters that users can customize
    parameters: list[RecipeParameter] = Field(
        default_factory=list, description="User-configurable parameters"
    )

    # The query template
    query_template: RecipeQueryTemplate = Field(
        description="Template for generating the data query"
    )

    # Interpretation prompt (how to present results)
    interpretation_prompt: str | None = Field(
        default=None, description="LLM prompt for interpreting results"
    )

    # Timestamps
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Usage stats
    execution_count: int = Field(default=0)
    last_executed_at: datetime | None = Field(default=None)

    # Sharing
    is_public: bool = Field(default=False, description="Whether this recipe is shared publicly")
    created_by: str | None = Field(default=None, description="User who created this recipe")


class RecipeExecutionStatus(StrEnum):
    """Status of a recipe execution."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class RecipeExecution(BaseModel):
    """
    A single execution of a recipe.

    Tracks when a recipe was run, with what parameters,
    and what the results were.
    """

    model_config = ConfigDict(use_enum_values=True)

    # Identity
    id: UUID = Field(default_factory=uuid4)
    recipe_id: UUID = Field(description="Which recipe was executed")
    tenant_id: str = Field(description="Tenant context")

    # Execution parameters
    parameter_values: dict[str, Any] = Field(
        default_factory=dict, description="Parameter values used for this execution"
    )

    # Status
    status: RecipeExecutionStatus = Field(default=RecipeExecutionStatus.PENDING)
    error_message: str | None = Field(default=None)

    # Results
    result_count: int | None = Field(default=None, description="Number of results returned")
    result_summary: str | None = Field(default=None, description="LLM-generated summary of results")
    aggregates: dict[str, Any] = Field(
        default_factory=dict, description="Computed aggregate values"
    )

    # Performance
    started_at: datetime | None = Field(default=None)
    completed_at: datetime | None = Field(default=None)
    duration_ms: float | None = Field(default=None)

    # Triggered by
    triggered_by: str | None = Field(
        default=None, description="User or system that triggered execution"
    )
