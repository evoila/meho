# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Recipe Executor Service.

Executes saved recipes with user-provided parameter values.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from meho_app.core.otel import get_logger
from meho_app.modules.agents.data_reduction.adapter import execute_data_query
from meho_app.modules.agents.data_reduction.query_schema import DataQuery, ReducedData
from meho_app.modules.agents.recipes.models import (
    Recipe,
    RecipeExecution,
    RecipeExecutionStatus,
)

logger = get_logger(__name__)


class RecipeExecutionError(Exception):
    """Error during recipe execution."""

    pass


class RecipeExecutor:
    """
    Executes recipes with user-provided parameters.

    The execution flow:
    1. Validate parameter values against recipe definition
    2. Render the query template with parameter values
    3. Call the API to get raw data
    4. Execute the data reduction query
    5. Return reduced results and execution metadata
    """

    def __init__(self):
        """Initialize the executor."""
        pass

    async def execute(
        self,
        recipe: Recipe,
        parameter_values: dict[str, Any],
        api_response: dict[str, Any],
        triggered_by: str | None = None,
    ) -> RecipeExecution:
        """
        Execute a recipe with the given parameters.

        Args:
            recipe: The recipe to execute
            parameter_values: Values for recipe parameters
            api_response: Raw API response data to process
            triggered_by: Who/what triggered this execution

        Returns:
            RecipeExecution with results
        """
        execution = RecipeExecution(
            recipe_id=recipe.id,
            tenant_id=recipe.tenant_id,
            parameter_values=parameter_values,
            status=RecipeExecutionStatus.PENDING,
            triggered_by=triggered_by,
        )

        try:
            # Mark as running
            execution.status = RecipeExecutionStatus.RUNNING
            execution.started_at = datetime.now(tz=UTC)

            # Validate parameters
            validated_params = self._validate_parameters(recipe, parameter_values)

            # Render the query template
            query = recipe.query_template.render(validated_params)

            # Execute data reduction
            start_time = time.perf_counter()
            result = execute_data_query(api_response, query)
            duration_ms = (time.perf_counter() - start_time) * 1000

            # Update execution with results
            execution.status = RecipeExecutionStatus.COMPLETED
            execution.completed_at = datetime.now(tz=UTC)
            execution.duration_ms = duration_ms
            execution.result_count = result.returned_records
            execution.aggregates = result.aggregates

            # Generate summary
            execution.result_summary = self._generate_summary(recipe, result)

            return execution

        except Exception as e:
            logger.exception(f"Recipe execution failed: {e}")
            execution.status = RecipeExecutionStatus.FAILED
            execution.error_message = str(e)
            execution.completed_at = datetime.now(tz=UTC)
            return execution

    def _validate_parameters(
        self,
        recipe: Recipe,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Validate parameter values against recipe definition.

        Args:
            recipe: The recipe being executed
            values: User-provided parameter values

        Returns:
            Validated and normalized parameter values

        Raises:
            RecipeExecutionError: If validation fails
        """
        validated = {}

        for param in recipe.parameters:
            name = param.name

            if name in values:
                value = values[name]
            elif param.default_value is not None:
                value = param.default_value
            elif param.required:
                raise RecipeExecutionError(f"Required parameter '{param.display_name}' is missing")
            else:
                continue  # Skip optional parameters without values

            # Type validation
            value = self._validate_type(param.param_type, value, param.display_name)

            # Range validation for numbers
            if param.min_value is not None and value < param.min_value:
                raise RecipeExecutionError(
                    f"Parameter '{param.display_name}' must be at least {param.min_value}"
                )
            if param.max_value is not None and value > param.max_value:
                raise RecipeExecutionError(
                    f"Parameter '{param.display_name}' must be at most {param.max_value}"
                )

            # Enum validation
            if param.allowed_values is not None and value not in param.allowed_values:
                raise RecipeExecutionError(
                    f"Parameter '{param.display_name}' must be one of: {param.allowed_values}"
                )

            validated[name] = value

        return validated

    def _validate_type(
        self,
        expected_type: str,
        value: Any,
        display_name: str,
    ) -> Any:
        """Validate and convert parameter value to expected type."""
        if expected_type == "string":
            return str(value)
        elif expected_type == "number":
            try:
                return float(value)
            except (ValueError, TypeError):
                raise RecipeExecutionError(f"Parameter '{display_name}' must be a number") from None
        elif expected_type == "integer":
            try:
                return int(value)
            except (ValueError, TypeError):
                raise RecipeExecutionError(
                    f"Parameter '{display_name}' must be an integer"
                ) from None
        elif expected_type == "boolean":
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.lower() in ("true", "yes", "1")
            return bool(value)
        elif expected_type == "array":
            if isinstance(value, list):
                return value
            return [value]
        else:
            return value

    def _generate_summary(
        self,
        recipe: Recipe,
        result: ReducedData,
    ) -> str:
        """Generate a human-readable summary of the results."""
        lines = []

        # Basic stats
        lines.append(
            f"Found {result.returned_records} results "
            f"(from {result.total_source_records} total records)"
        )

        # Aggregates
        if result.aggregates:
            lines.append("\nSummary Statistics:")
            for name, value in result.aggregates.items():
                if isinstance(value, float):
                    lines.append(f"  {name}: {value:.2f}")
                else:
                    lines.append(f"  {name}: {value}")

        # Truncation warning
        if result.is_truncated:
            lines.append(
                f"\nNote: Results were limited to {result.returned_records} "
                f"({result.total_after_filter} matched the filter)"
            )

        return "\n".join(lines)

    def preview(
        self,
        recipe: Recipe,
        parameter_values: dict[str, Any],
    ) -> DataQuery:
        """
        Preview the query that would be executed.

        Args:
            recipe: The recipe
            parameter_values: Parameter values

        Returns:
            The rendered DataQuery (not executed)
        """
        validated_params = self._validate_parameters(recipe, parameter_values)
        return recipe.query_template.render(validated_params)


class RecipeScheduler:
    """
    Schedules recipe executions.

    This is a placeholder for future scheduling capabilities.
    Recipes could be scheduled to run:
    - On a cron schedule
    - When triggered by events
    - As part of a larger workflow
    """

    def __init__(self, executor: RecipeExecutor):
        """Initialize the scheduler."""
        self.executor = executor
        self._schedules: dict[UUID, dict] = {}

    async def schedule(
        self,
        recipe_id: UUID,
        cron_expression: str,
        parameter_values: dict[str, Any],
    ) -> dict:
        """
        Schedule a recipe for periodic execution.

        Args:
            recipe_id: Recipe to schedule
            cron_expression: Cron schedule (e.g., "0 * * * *" for hourly)
            parameter_values: Default parameter values

        Returns:
            Schedule metadata
        """
        schedule = {
            "recipe_id": recipe_id,
            "cron": cron_expression,
            "parameters": parameter_values,
            "enabled": True,
            "created_at": datetime.now(tz=UTC),
        }
        self._schedules[recipe_id] = schedule
        return schedule

    async def unschedule(self, recipe_id: UUID) -> bool:
        """Remove a scheduled recipe."""
        if recipe_id in self._schedules:
            del self._schedules[recipe_id]
            return True
        return False

    async def list_schedules(self) -> list[dict]:
        """List all scheduled recipes."""
        return list(self._schedules.values())
