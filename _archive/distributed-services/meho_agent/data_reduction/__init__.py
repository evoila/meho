"""
Data Reduction Engine for MEHO.

This module provides server-side data processing capabilities that enable
LLMs to work with large API responses without exceeding context limits.

The core principle: LLM generates a query plan, server executes it,
LLM interprets the reduced results.

Components:
- query_schema: Pydantic models for the Query DSL
- engine: DataReductionEngine that executes queries
- operators: Filter, sort, and aggregate operators
"""

from meho_agent.data_reduction.query_schema import (
    DataQuery,
    ReducedData,
    FilterCondition,
    FilterGroup,
    FilterOperator,
    AggregateFunction,
    SortSpec,
    AggregateSpec,
    ComputeField,
)
from meho_agent.data_reduction.engine import DataReductionEngine, DataReductionError
from meho_agent.data_reduction.query_generator import (
    QueryGeneratorContext,
    QueryGeneratorOutput,
    generate_query,
    get_query_generator_agent,
    validate_query_against_schema,
)

__all__ = [
    # Query Schema
    "DataQuery",
    "ReducedData",
    "FilterCondition",
    "FilterGroup",
    "FilterOperator",
    "AggregateFunction",
    "SortSpec",
    "AggregateSpec",
    "ComputeField",
    # Engine
    "DataReductionEngine",
    "DataReductionError",
    # Query Generator
    "QueryGeneratorContext",
    "QueryGeneratorOutput",
    "generate_query",
    "get_query_generator_agent",
    "validate_query_against_schema",
]


