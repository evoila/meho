# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""JSONFlux-powered data aggregation for agent workflows.

Provides NLQ-to-SQL translation via a dedicated inline LLM, using JSONFlux's
auto-generated system prompts and QueryEngine for execution. The agent never
touches raw data -- it sees schema previews and issues natural language queries.

Usage (Phase 2 will wire this into ReduceDataNode):

    engine = QueryEngine()
    engine.register("pods", pod_data)
    engine.register("namespaces", ns_data)

    # Agent sees this in its context (~200 tokens):
    preview = generate_data_preview(engine)

    # Agent issues NLQ, gets markdown back:
    result = await jsonflux_aggregate(
        engine,
        "Filter pods in CrashLoopBackOff, group by namespace",
    )
    if result.success:
        print(result.markdown)  # Formatted markdown table
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import BaseModel

from meho_app.jsonflux import QueryEngine

logger = logging.getLogger(__name__)

# Error prefix used by QueryEngine.format_query() on SQL failures.
_ERROR_PREFIX = "ERROR: "

# Table names too generic to be useful for SQL queries.  When a table name
# (derived from operation_id via ``_derive_table_name()``) is in this set,
# ``_infer_table_name()`` inspects the JSON structure to find a better name.
GENERIC_NAMES: frozenset[str] = frozenset(
    {
        "data",
        "items",
        "resources",
        "results",
        "records",
        "entries",
        "objects",
    }
)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class AggregationResult:
    """Result of a JSONFlux aggregation query.

    On success: ``success=True``, ``markdown`` contains the formatted table,
    ``sql`` contains the executed SQL.

    On failure: ``success=False``, ``error`` contains the description,
    ``sql`` contains the last attempted SQL.
    """

    success: bool
    markdown: str = ""
    sql: str = ""
    error: str = ""
    row_count: int = 0


class _SQLGenerationResult(BaseModel):
    """Pydantic model for structured LLM output: NLQ -> SQL."""

    sql: str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_data_preview(engine: QueryEngine) -> str:
    """Generate a lightweight schema preview for the agent's context.

    Returns compact TypeScript-like schemas with row counts and sample
    values. The agent uses this to understand what data is available
    and to formulate natural language queries.

    Example output::

        ## Available Data

        ### pods (15000 rows)
        {metadata: {name: str, namespace: str}, status: {phase: str, ...}}

        ### namespaces (100 rows)
        {metadata: {name: str, labels: {str: str}}, status: {phase: str}}

    This is ~200 tokens for two tables, vs 500,000+ for raw JSON.

    Args:
        engine: QueryEngine with tables already registered.

    Returns:
        Markdown string with table schemas, or empty string if no tables.
    """
    return engine.describe_tables(samples=2)


async def jsonflux_aggregate(
    engine: QueryEngine,
    natural_language_query: str,
    max_retries: int = 3,
    format: str = "markdown",
    max_rows: int | None = 100,
) -> AggregationResult:
    """Translate a natural language query to SQL and execute it.

    The agent calls this with a plain English query like:
        "Filter pods where status.phase is CrashLoopBackOff, group by namespace"

    Behind the scenes:

    1. ``engine.generate_prompt()`` creates a comprehensive SQL system prompt
       (schema, UNNEST patterns, JOIN examples, common mistakes).
    2. A dedicated inline LLM translates the NLQ to DuckDB SQL.
    3. ``engine.format_query()`` executes the SQL and returns formatted output.
    4. On SQL error: retry with error context (up to ``max_retries``).

    Args:
        engine: QueryEngine with tables already registered (from Redis cache).
        natural_language_query: What the agent wants. Plain English.
        max_retries: Max LLM retry attempts on SQL error.
        format: Output format (markdown, grid, csv, json).
        max_rows: Max rows in output (None = unlimited).

    Returns:
        AggregationResult with markdown data or error description.
    """
    system_prompt = engine.generate_prompt()

    last_error: str | None = None
    sql = ""

    for attempt in range(max_retries):
        sql = await _generate_sql(system_prompt, natural_language_query, last_error)

        result = engine.format_query(sql, format=format, max_rows=max_rows, max_colwidth=None)

        if not result.startswith(_ERROR_PREFIX):
            row_count = _count_table_rows(result)
            return AggregationResult(
                success=True,
                markdown=result,
                sql=sql,
                row_count=row_count,
            )

        # SQL error -- feed it back for retry
        last_error = result
        logger.warning(
            "JSONFlux SQL attempt %d/%d failed: %s",
            attempt + 1,
            max_retries,
            result,
        )

    # All retries exhausted
    return AggregationResult(
        success=False,
        error=last_error or "Unknown error",
        sql=sql,
    )


# ---------------------------------------------------------------------------
# Table name inference
# ---------------------------------------------------------------------------


def _infer_table_name(
    data: list | dict, fallback: str = "data"
) -> str:  # NOSONAR (cognitive complexity)
    """Infer a meaningful table name from JSON structure.

    Used when ``_derive_table_name()`` (from operation_id) produces something
    generic like ``"resources"`` or ``"items"``. Checks JSON conventions to
    find a better name.

    Strategy:

    1. List of dicts with ``"kind"`` field (K8s): ``"Pod"`` -> ``"pods"``
    2. Dict with single key containing list: ``{"namespaces": [...]}`` -> ``"namespaces"``
    3. K8s list pattern: ``{"kind": "PodList", "items": [...]}`` -> ``"pods"``
    4. Fallback to provided name.

    Args:
        data: The raw JSON data (list or dict).
        fallback: Name to use if no heuristic matches.

    Returns:
        A lowercase, pluralized table name.
    """
    # Strategy 1: List of dicts with "kind" field (K8s resources)
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict) and "kind" in first:
            kind: str = str(first["kind"]).lower()
            if not kind.endswith("s"):
                kind += "s"
            return kind

    if isinstance(data, dict):
        # Strategy 2: Dict with single key containing a list
        keys = list(data.keys())
        if len(keys) == 1 and isinstance(data[keys[0]], list):
            return str(keys[0]).lower()

        # Strategy 3: K8s list wrapper {"kind": "PodList", "items": [...]}
        if "items" in data and isinstance(data["items"], list):
            # Try the top-level "kind" field (e.g. "PodList" -> "pods")
            if "kind" in data and isinstance(data["kind"], str):
                k8s_kind: str = data["kind"].lower().replace("list", "").strip()
                if k8s_kind and not k8s_kind.endswith("s"):
                    k8s_kind += "s"
                if k8s_kind:
                    return k8s_kind

            # Fallback: check the first item's "kind" field
            if data["items"]:
                first_item = data["items"][0]
                if isinstance(first_item, dict) and "kind" in first_item:
                    item_kind: str = str(first_item["kind"]).lower()
                    if not item_kind.endswith("s"):
                        item_kind += "s"
                    return item_kind

    return fallback


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


async def _generate_sql(
    system_prompt: str,
    natural_language_query: str,
    last_error: str | None = None,
) -> str:
    """Generate SQL from a natural language query using a dedicated inline LLM.

    Args:
        system_prompt: JSONFlux auto-generated system prompt with schema,
            UNNEST patterns, JOIN examples, DuckDB functions.
        natural_language_query: The agent's plain English query.
        last_error: Error from a previous attempt (for retry context).

    Returns:
        SQL string generated by the LLM.
    """
    from meho_app.modules.agents.base.inference import infer_structured

    # Build user prompt
    parts = [f"Write a DuckDB SQL query for: {natural_language_query}"]
    if last_error:
        parts.append(
            f"\nThe previous SQL attempt failed with this error:\n{last_error}\n"
            "Please fix the SQL to avoid this error."
        )

    user_prompt = "\n".join(parts)

    result = await infer_structured(
        prompt=user_prompt,
        response_model=_SQLGenerationResult,
        instructions=system_prompt,
        temperature=0.0,
    )

    return result.sql


def _count_table_rows(formatted_output: str) -> int:
    """Estimate row count from a formatted table string.

    Counts lines that look like table data rows (start with ``|``)
    and subtracts 2 for the header row and separator line.
    """
    if not formatted_output or not formatted_output.strip():
        return 0

    table_lines = [line for line in formatted_output.strip().split("\n") if line.startswith("|")]

    # Markdown tables have: header | separator | data rows
    # Subtract 2 for header and separator (e.g. |---|---|)
    row_count = max(0, len(table_lines) - 2)
    return row_count
