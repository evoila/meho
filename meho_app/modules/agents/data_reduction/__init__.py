# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Data Reduction for MEHO.

This module provides server-side data processing capabilities that enable
LLMs to work with large API responses without exceeding context limits.

The core principle: LLM generates a query plan, server executes it,
LLM interprets the reduced results.

Components:
- query_schema: Pydantic models for the Query DSL
- adapter: Executes DataQuery via QueryEngine (DuckDB/Arrow)
- query_generator: LLM agent that generates DataQuery from natural language
"""

from meho_app.modules.agents.data_reduction.adapter import (
    DataReductionError,
    execute_data_query,
)
from meho_app.modules.agents.data_reduction.query_generator import (
    QueryGeneratorContext,
    QueryGeneratorOutput,
    generate_query,
    get_query_generator_agent,
    validate_query_against_schema,
)
from meho_app.modules.agents.data_reduction.query_schema import (
    AggregateFunction,
    AggregateSpec,
    ComputeField,
    DataQuery,
    FilterCondition,
    FilterGroup,
    FilterOperator,
    ReducedData,
    SortSpec,
)

__all__ = [
    "AggregateFunction",
    "AggregateSpec",
    "ComputeField",
    # Query Schema
    "DataQuery",
    # Adapter
    "DataReductionError",
    "FilterCondition",
    "FilterGroup",
    "FilterOperator",
    # Query Generator
    "QueryGeneratorContext",
    "QueryGeneratorOutput",
    "ReducedData",
    "SortSpec",
    "execute_data_query",
    "generate_query",
    "get_query_generator_agent",
    "validate_query_against_schema",
]
