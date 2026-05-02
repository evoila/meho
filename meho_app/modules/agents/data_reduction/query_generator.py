# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Query Generator - LLM Agent for Data Reduction.

This module uses PydanticAI to generate DataQuery specifications
from natural language questions and API response schemas.

The LLM doesn't see the actual data - only the schema. It generates
a query that the DataReductionEngine will execute server-side.

Example Flow:
1. User: "Show me clusters with memory usage over 80%"
2. LLM sees: {schema of /clusters response}
3. LLM generates: DataQuery(filter=[memory_pct > 80], sort=memory_pct DESC)
4. Server executes query on 500 clusters → returns 12 matching
5. LLM interprets: "Found 12 clusters with high memory usage..."
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext

from meho_app.core.otel import get_logger
from meho_app.modules.agents.data_reduction.query_schema import (
    DataQuery,
    FilterCondition,
)

logger = get_logger(__name__)


# =============================================================================
# Context for the Query Generator
# =============================================================================


@dataclass
class QueryGeneratorContext:
    """Context provided to the query generator agent."""

    # The user's question
    question: str

    # Schema of the API response (what fields are available)
    response_schema: dict[str, Any]

    # Sample data (optional, for understanding field types/values)
    sample_data: dict[str, Any] | None = None

    # Endpoint info for context
    endpoint_path: str | None = None
    endpoint_method: str | None = None

    # Maximum records to return (for limit guidance)
    max_records: int = 100


# =============================================================================
# Output Model
# =============================================================================


class QueryGeneratorOutput(BaseModel):
    """Output from the query generator."""

    query: DataQuery = Field(description="The generated data query")
    reasoning: str = Field(description="Brief explanation of why this query was generated")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence in the query (0-1)")


# =============================================================================
# System Prompt
# =============================================================================

QUERY_GENERATOR_SYSTEM_PROMPT = """You are a data query generator for the MEHO diagnostic system.

Your job is to translate natural language questions into structured data queries.
You receive:
1. A user's question about the data
2. The schema of the API response (available fields)
3. Optionally, sample data to understand field types

You generate a DataQuery that specifies:
- source_path: Where to find records in the response (e.g., "clusters", "data.items")
- select: Which fields to include (or null for all)
- compute: Derived fields to calculate (e.g., percentages, ratios)
- filter: Conditions to filter records
- sort: How to order results
- limit: Maximum records to return
- aggregates: Summary statistics (count, sum, avg, etc.)

## Key Principles

1. **Be conservative with limits** - Start with 20-50 records unless user asks for all
2. **Compute useful derived fields** - If user asks about percentages, compute them
3. **Always sort by relevance** - If filtering by a metric, sort by that metric
4. **Include useful aggregates** - Add count and relevant stats automatically
5. **Handle nested data** - Use dot notation for nested fields (e.g., "metadata.name")

## Filter Operators

- Comparison: =, !=, >, >=, <, <=
- String: contains, starts_with, ends_with, matches (regex)
- Collection: in, not_in
- Null: is_null, is_not_null

## Aggregate Functions

- count, sum, avg, min, max
- first, last, median, std, var
- collect (gather into list), distinct (unique values)

## Examples

Question: "Show clusters with high memory usage"
Schema: {clusters: [{name, region, memory_total_gb, memory_used_gb, status}]}
Query:
- source_path: "clusters"
- compute: [{name: "memory_pct", expression: "memory_used_gb / memory_total_gb * 100"}]
- filter: {conditions: [{field: "memory_pct", operator: ">", value: 80}]}
- sort: {field: "memory_pct", direction: "desc"}
- limit: 20
- aggregates: [{name: "avg_usage", function: "avg", field: "memory_pct"}]

Question: "How many pods are in each namespace?"
Schema: {items: [{metadata: {name, namespace}, status: {phase}}]}
Query:
- source_path: "items"
- select: ["metadata.namespace"]
- group_by: ["metadata.namespace"]
- aggregates: [{name: "count", function: "count", field: "*"}]

Question: "List all failed deployments"
Schema: {deployments: [{name, status, replicas, ready_replicas}]}
Query:
- source_path: "deployments"
- filter: {conditions: [{field: "status", operator: "=", value: "Failed"}]}
- sort: {field: "name", direction: "asc"}
- limit: 50

## Important Notes

- If the schema shows nested objects, use dot notation in filters and selects
- If you need to compute percentages/ratios, add compute fields
- Always include relevant aggregates to give the user summary statistics
- If the question is vague, make reasonable assumptions and include more fields
"""


# =============================================================================
# Agent Factory
# =============================================================================

_query_generator_agent: Agent[QueryGeneratorContext, QueryGeneratorOutput] | None = None


def get_query_generator_agent() -> Agent[
    QueryGeneratorContext, QueryGeneratorOutput
]:  # NOSONAR (cognitive complexity)
    """
    Get or create the query generator agent.

    Uses lazy initialization to avoid loading config at import time.
    """
    global _query_generator_agent

    if _query_generator_agent is None:
        from pydantic_ai import InstrumentationSettings

        from meho_app.core.config import get_config

        agent = Agent(
            model=get_config().data_extractor_model,
            output_type=QueryGeneratorOutput,
            deps_type=QueryGeneratorContext,
            instructions=QUERY_GENERATOR_SYSTEM_PROMPT,
            instrument=InstrumentationSettings(),
        )

        # Register tools
        @agent.tool
        def get_available_fields(ctx: RunContext[QueryGeneratorContext]) -> str:
            """Get the list of available fields from the response schema."""
            schema = ctx.deps.response_schema

            def extract_fields(obj: Any, prefix: str = "") -> list[str]:
                """Recursively extract field names from schema."""
                fields = []

                if isinstance(obj, dict):
                    if "properties" in obj:
                        # JSON Schema format
                        for name, prop in obj.get("properties", {}).items():
                            full_name = f"{prefix}{name}" if prefix else name
                            fields.append(full_name)
                            if isinstance(prop, dict):
                                if prop.get("type") == "object":
                                    fields.extend(extract_fields(prop, f"{full_name}."))
                                elif prop.get("type") == "array" and "items" in prop:
                                    fields.extend(extract_fields(prop["items"], f"{full_name}[]."))
                    else:
                        # Plain dict format
                        for name, value in obj.items():
                            full_name = f"{prefix}{name}" if prefix else name
                            fields.append(full_name)
                            if isinstance(value, dict):
                                fields.extend(extract_fields(value, f"{full_name}."))
                            elif isinstance(value, list) and value and isinstance(value[0], dict):
                                fields.extend(extract_fields(value[0], f"{full_name}[]."))

                return fields

            fields = extract_fields(schema)
            return f"Available fields: {', '.join(fields)}"

        @agent.tool
        def get_sample_values(ctx: RunContext[QueryGeneratorContext]) -> str:
            """Get sample values from the data to understand field types."""
            if not ctx.deps.sample_data:
                return "No sample data available"

            sample = ctx.deps.sample_data

            def get_samples(obj: Any, prefix: str = "", depth: int = 0) -> list[str]:
                """Get sample values from data."""
                if depth > 3:  # Limit depth
                    return []

                samples = []

                if isinstance(obj, dict):
                    for key, value in list(obj.items())[:10]:  # Limit keys
                        full_key = f"{prefix}{key}" if prefix else key
                        if isinstance(value, (str, int, float, bool)):
                            samples.append(f"{full_key}: {repr(value)[:50]}")
                        elif isinstance(value, dict):
                            samples.extend(get_samples(value, f"{full_key}.", depth + 1))
                        elif isinstance(value, list) and value:
                            samples.append(f"{full_key}: list[{len(value)} items]")
                            if isinstance(value[0], dict):
                                samples.extend(get_samples(value[0], f"{full_key}[].", depth + 1))

                return samples

            samples = get_samples(sample)
            return "Sample values:\n" + "\n".join(samples[:20])

        _query_generator_agent = agent

    return _query_generator_agent


# =============================================================================
# High-Level API
# =============================================================================


async def generate_query(
    question: str,
    response_schema: dict[str, Any],
    sample_data: dict[str, Any] | None = None,
    endpoint_path: str | None = None,
    max_records: int = 100,
) -> QueryGeneratorOutput:
    """
    Generate a DataQuery from a natural language question.

    Args:
        question: The user's question about the data
        response_schema: Schema of the API response
        sample_data: Optional sample data for context
        endpoint_path: Optional endpoint path for context
        max_records: Maximum records to return

    Returns:
        QueryGeneratorOutput with the generated query
    """
    context = QueryGeneratorContext(
        question=question,
        response_schema=response_schema,
        sample_data=sample_data,
        endpoint_path=endpoint_path,
        max_records=max_records,
    )

    prompt = f"""Generate a data query for this question:

Question: {question}

Response Schema:
{json.dumps(response_schema, indent=2)}

Endpoint: {endpoint_path or "Unknown"}
Max Records: {max_records}
"""

    if sample_data:
        # Add truncated sample for context
        sample_str = json.dumps(sample_data, indent=2)[:1000]
        prompt += f"\nSample Data (truncated):\n{sample_str}"

    agent = get_query_generator_agent()
    result = await agent.run(prompt, deps=context)
    return result.output


# =============================================================================
# Query Validation
# =============================================================================


def validate_query_against_schema(  # NOSONAR (cognitive complexity)
    query: DataQuery,
    schema: dict[str, Any],
) -> list[str]:
    """
    Validate a generated query against the response schema.

    Returns list of warnings (empty if valid).
    """
    warnings = []

    # This is a basic validation - could be expanded
    def get_schema_fields(obj: Any) -> set[str]:
        """Extract field names from schema."""
        fields = set()
        if isinstance(obj, dict):
            for key in obj:
                fields.add(key)
                if isinstance(obj[key], dict):
                    for subkey in get_schema_fields(obj[key]):
                        fields.add(f"{key}.{subkey}")
        return fields

    schema_fields = get_schema_fields(schema)

    # Check selected fields
    if query.select:
        for field in query.select:
            base_field = field.split(".")[0]
            if base_field not in schema_fields and field not in schema_fields:
                warnings.append(f"Selected field '{field}' may not exist in schema")

    # Check filter fields
    if query.filter:
        for condition in query.filter.conditions:
            if isinstance(condition, FilterCondition):
                base_field = condition.field.split(".")[0]
                if base_field not in schema_fields and condition.field not in schema_fields:
                    warnings.append(f"Filter field '{condition.field}' may not exist in schema")

    return warnings
