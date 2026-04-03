# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Query DSL Schema for Data Reduction Engine.

This module defines the Pydantic models that represent the query language
used by LLMs to specify data extraction, filtering, transformation, and
aggregation operations.

Design Goals:
1. Simple enough for LLMs to generate reliably
2. Expressive enough for real-world use cases
3. Safe to execute (no arbitrary code execution)
4. Efficient to process (leverage pandas/polars)

Example Query (what LLM generates):
```json
{
  "source_path": "clusters",
  "select": ["name", "region", "memory_used_gb", "memory_total_gb"],
  "compute": [
    {"name": "memory_pct", "expression": "memory_used_gb / memory_total_gb * 100"}
  ],
  "filter": {
    "conditions": [
      {"field": "memory_pct", "operator": ">", "value": 80}
    ],
    "logic": "and"
  },
  "sort": {"field": "memory_pct", "direction": "desc"},
  "limit": 20,
  "aggregates": [
    {"name": "avg_memory_pct", "function": "avg", "field": "memory_pct"},
    {"name": "total_clusters", "function": "count", "field": "*"}
  ]
}
```
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FilterOperator(StrEnum):
    """Supported filter operators."""

    # Comparison
    EQ = "="
    NE = "!="
    GT = ">"
    GTE = ">="
    LT = "<"
    LTE = "<="

    # String operations
    CONTAINS = "contains"
    STARTS_WITH = "starts_with"
    ENDS_WITH = "ends_with"
    MATCHES = "matches"  # Regex

    # Collection operations
    IN = "in"
    NOT_IN = "not_in"

    # Null checks
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"


class AggregateFunction(StrEnum):
    """Supported aggregate functions."""

    COUNT = "count"
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"
    FIRST = "first"
    LAST = "last"

    # Statistical
    MEDIAN = "median"
    STD = "std"
    VAR = "var"

    # Collection
    COLLECT = "collect"  # Gather into list
    DISTINCT = "distinct"  # Unique values


class FilterCondition(BaseModel):
    """A single filter condition."""

    model_config = ConfigDict(use_enum_values=True)

    field: str = Field(
        description="The field to filter on (supports dot notation for nested fields)"
    )
    operator: FilterOperator = Field(description="The comparison operator")
    value: Any | None = Field(
        default=None,
        description="The value to compare against (not needed for is_null/is_not_null)",
    )


class FilterGroup(BaseModel):
    """A group of filter conditions combined with AND/OR logic."""

    conditions: list[FilterCondition | FilterGroup] = Field(
        description="List of conditions or nested groups"
    )
    logic: Literal["and", "or"] = Field(default="and", description="How to combine conditions")


class ComputeField(BaseModel):
    """A computed/derived field."""

    name: str = Field(description="Name of the new field")
    expression: str = Field(
        description=(
            "Expression to compute the field. Supports basic arithmetic "
            "(+, -, *, /), field references, and safe functions."
        )
    )

    @field_validator("expression")
    @classmethod
    def validate_expression(cls, v: str) -> str:
        """Validate expression doesn't contain dangerous operations."""
        # Disallow imports, exec, eval, etc.
        dangerous = ["import", "exec", "eval", "__", "globals", "locals", "open", "file"]
        v_lower = v.lower()
        for d in dangerous:
            if d in v_lower:
                raise ValueError(f"Expression contains forbidden term: {d}")
        return v


class SortSpec(BaseModel):
    """Sort specification."""

    field: str = Field(description="Field to sort by")
    direction: Literal["asc", "desc"] = Field(default="desc", description="Sort direction")


class AggregateSpec(BaseModel):
    """Aggregation specification."""

    model_config = ConfigDict(use_enum_values=True)

    name: str = Field(description="Name for the aggregated result")
    function: AggregateFunction = Field(description="Aggregation function to apply")
    field: str = Field(description="Field to aggregate (use '*' for count)")


class DataQuery(BaseModel):
    """
    Complete query specification for data reduction.

    This is what the LLM generates based on the user's question
    and the API response schema.
    """

    # Source extraction
    source_path: str = Field(
        default="",
        description=(
            "JSONPath-like path to extract records from API response. "
            "Empty string means root. Examples: 'clusters', 'data.items', 'results[*]'"
        ),
    )

    # Field selection
    select: list[str] | None = Field(
        default=None,
        description=(
            "Fields to include in output. None means all fields. "
            "Supports dot notation for nested fields."
        ),
    )

    # Computed fields
    compute: list[ComputeField] = Field(
        default_factory=list, description="Derived fields to compute from existing data"
    )

    # Filtering
    filter: FilterGroup | None = Field(default=None, description="Filter conditions to apply")

    # Sorting
    sort: SortSpec | None = Field(default=None, description="Sort specification")

    # Pagination
    limit: int | None = Field(
        default=None, ge=1, le=10000, description="Maximum number of records to return"
    )
    offset: int | None = Field(default=None, ge=0, description="Number of records to skip")

    # Aggregations
    aggregates: list[AggregateSpec] = Field(
        default_factory=list, description="Aggregations to compute over the filtered data"
    )

    # Grouping (for grouped aggregations)
    group_by: list[str] | None = Field(
        default=None, description="Fields to group by for aggregations"
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "source_path": "clusters",
                    "select": ["name", "region", "memory_used_gb", "memory_total_gb"],
                    "compute": [
                        {
                            "name": "memory_pct",
                            "expression": "memory_used_gb / memory_total_gb * 100",
                        }
                    ],
                    "filter": {
                        "conditions": [{"field": "memory_pct", "operator": ">", "value": 80}],
                        "logic": "and",
                    },
                    "sort": {"field": "memory_pct", "direction": "desc"},
                    "limit": 20,
                    "aggregates": [
                        {"name": "avg_memory_pct", "function": "avg", "field": "memory_pct"},
                        {"name": "total_clusters", "function": "count", "field": "*"},
                    ],
                }
            ]
        }
    )


class ReducedData(BaseModel):
    """
    Result of executing a DataQuery.

    This is what gets sent to the LLM for interpretation -
    a small, focused dataset ready for reasoning.
    """

    # The filtered/processed records
    records: list[dict[str, Any]] = Field(description="The processed records matching the query")

    # Metadata about the reduction
    total_source_records: int = Field(description="Total records in the source before filtering")
    total_after_filter: int = Field(description="Records remaining after filtering (before limit)")
    returned_records: int = Field(description="Number of records returned (after limit)")

    # Aggregation results
    aggregates: dict[str, Any] = Field(
        default_factory=dict, description="Computed aggregate values"
    )

    # Query metadata
    query_applied: DataQuery = Field(description="The query that was executed")

    # Processing stats
    processing_time_ms: float = Field(description="Time taken to process the data")

    @property
    def is_truncated(self) -> bool:
        """Whether the results were truncated by limit."""
        return self.returned_records < self.total_after_filter

    @property
    def reduction_ratio(self) -> float:
        """How much the data was reduced (1.0 = no reduction)."""
        if self.total_source_records == 0:
            return 1.0
        return self.returned_records / self.total_source_records

    def to_llm_context(self) -> str:
        """Format for inclusion in LLM context."""
        lines = [
            f"Query Results ({self.returned_records} of {self.total_source_records} records):",
        ]

        if self.is_truncated:
            lines.append(
                f"  (Limited to {self.returned_records}, {self.total_after_filter} total matched)"
            )

        if self.aggregates:
            lines.append("Aggregates:")
            for name, value in self.aggregates.items():
                lines.append(f"  {name}: {value}")

        lines.append("Records:")
        for i, record in enumerate(self.records[:50]):  # Cap at 50 for display
            lines.append(f"  {i + 1}. {record}")

        if len(self.records) > 50:
            lines.append(f"  ... and {len(self.records) - 50} more")

        return "\n".join(lines)


# Enable forward references
FilterGroup.model_rebuild()
