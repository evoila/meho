# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
DataQuery-to-QueryEngine adapter.

Bridges the DataQuery DSL (used by LLM-generated queries) to the
DuckDB-based QueryEngine from jsonflux. This replaces the deleted
pandas-based DataReductionEngine.

The flow:
1. Extract source records from API response using source_path
2. Register data with a QueryEngine instance
3. Translate DataQuery fields (select, compute, filter, sort, etc.) to SQL
4. Execute via QueryEngine and return ReducedData
"""

from __future__ import annotations

import re
import time
from typing import Any

from meho_app.core.otel import get_logger
from meho_app.jsonflux import QueryEngine
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

logger = get_logger(__name__)


class DataReductionError(Exception):
    """Error during data reduction."""

    pass


def execute_data_query(
    data: dict[str, Any] | list,
    query: DataQuery,
    max_records: int = 10000,
    max_output_records: int = 1000,
) -> ReducedData:
    """Execute a DataQuery against data using QueryEngine (DuckDB).

    This replaces the deleted DataReductionEngine. It translates DataQuery
    fields to SQL and executes via QueryEngine.

    Args:
        data: The raw API response data (dict or list)
        query: The DataQuery specification
        max_records: Maximum records to process (safety limit)
        max_output_records: Maximum records to return

    Returns:
        ReducedData with processed results
    """
    start = time.perf_counter()

    try:
        # 1. Extract source records from data using query.source_path
        records = _extract_source(data, query.source_path)
        total_source = len(records)

        if total_source > max_records:
            logger.warning(
                f"Source has {total_source} records, truncating to {max_records}"
            )
            records = records[:max_records]

        if not records:
            return ReducedData(
                records=[],
                total_source_records=total_source,
                total_after_filter=0,
                returned_records=0,
                query_applied=query,
                processing_time_ms=(time.perf_counter() - start) * 1000,
            )

        # 2. Flatten nested dicts for DuckDB compatibility
        flat_records = [_flatten_record(r) for r in records]

        # 3. Register data with QueryEngine
        engine = QueryEngine()
        engine.register("src", flat_records)

        # 4. Handle computed fields by adding them via SQL expressions
        compute_exprs = _build_compute_expressions(query.compute)

        # 5. Build the main SQL query
        sql = _build_sql(query, compute_exprs, max_output_records)
        logger.debug(f"Generated SQL: {sql}")

        # 6. Execute and get results
        result_records = engine.query(sql)

        # 7. Get total_after_filter (count without LIMIT/OFFSET)
        count_sql = _build_count_sql(query, compute_exprs)
        count_result = engine.query(count_sql)
        total_after_filter = count_result[0]["cnt"] if count_result else 0

        # 8. Compute aggregates if requested
        aggregates: dict[str, Any] = {}
        if query.aggregates:
            aggregates = _compute_aggregates(
                engine, query.aggregates, query, compute_exprs
            )

        # 9. Unflatten record keys back to original dotted form
        result_records = [_unflatten_record(r) for r in result_records]

        processing_time = (time.perf_counter() - start) * 1000

        return ReducedData(
            records=result_records,
            total_source_records=total_source,
            total_after_filter=total_after_filter,
            returned_records=len(result_records),
            aggregates=aggregates,
            query_applied=query,
            processing_time_ms=processing_time,
        )

    except Exception as e:
        logger.exception(f"Data reduction failed: {e}")
        raise DataReductionError(f"Query execution failed: {e}") from e


# ---------------------------------------------------------------------------
# Source extraction
# ---------------------------------------------------------------------------


def _extract_source(
    data: dict[str, Any] | list,
    source_path: str,
) -> list[dict[str, Any]]:
    """Extract records from the source path.

    Reuses the same nested-dict navigation logic from the old
    DataReductionEngine.
    """
    if not source_path:
        if isinstance(data, list):
            return list(data)
        elif isinstance(data, dict):
            for key in ("data", "items", "results", "records"):
                if key in data and isinstance(data[key], list):
                    return list(data[key])
            return [dict(data)]
        return []

    current: Any = data
    parts = source_path.replace("[*]", "").split(".")

    for part in parts:
        if not part:
            continue
        if isinstance(current, dict):
            current = current.get(part, [])
        elif isinstance(current, list) and current:
            current = [
                item.get(part, {}) for item in current if isinstance(item, dict)
            ]
        else:
            return []

    if isinstance(current, list):
        return current
    elif isinstance(current, dict):
        return [current]
    return []


# ---------------------------------------------------------------------------
# Record flattening (nested dicts -> underscore-separated keys)
# ---------------------------------------------------------------------------


def _flatten_record(record: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten nested dicts using underscore separator for DuckDB column names.

    Example: {"metadata": {"name": "pod-1"}} -> {"metadata__name": "pod-1"}

    We use double underscore to avoid collision with single-underscore field names.
    """
    flat: dict[str, Any] = {}
    for key, value in record.items():
        full_key = f"{prefix}__{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(_flatten_record(value, full_key))
        else:
            flat[full_key] = value
    return flat


def _unflatten_record(record: dict[str, Any]) -> dict[str, Any]:
    """Convert flattened keys back to nested dicts using dot notation.

    Example: {"metadata__name": "pod-1"} -> {"metadata.name": "pod-1"}
    This preserves the original dotted-key convention for downstream consumers.
    """
    result: dict[str, Any] = {}
    for key, value in record.items():
        if "__" in key:
            result[key.replace("__", ".")] = value
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# SQL generation
# ---------------------------------------------------------------------------

# Map field references using dot notation to flattened column names
def _field_to_col(field: str) -> str:
    """Convert a dotted field name to a DuckDB-safe column name."""
    if "." in field:
        return '"' + field.replace(".", "__") + '"'
    return f'"{field}"'


def _build_compute_expressions(computes: list[ComputeField]) -> list[str]:
    """Build SQL expressions for computed fields.

    Returns a list of SQL expression strings like: `expr AS name`
    """
    exprs = []
    for c in computes:
        # Replace dotted field references in the expression with flattened names
        expr = _translate_expression(c.expression)
        exprs.append(f"({expr}) AS \"{c.name}\"")
    return exprs


def _translate_expression(expression: str) -> str:
    """Translate a compute expression to DuckDB SQL.

    Handles field references that may use dot notation.
    """
    # Replace field references with quoted column names
    # Match word sequences that look like field names (word.word patterns)
    def _replace_field(m: re.Match) -> str:
        field = m.group(0)
        if "." in field:
            return '"' + field.replace(".", "__") + '"'
        return f'"{field}"'

    # Match identifiers (possibly dotted)
    return re.sub(r"[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*", _replace_field, expression)


def _build_sql(
    query: DataQuery,
    compute_exprs: list[str],
    max_output_records: int,
) -> str:
    """Build the main SELECT SQL from a DataQuery."""
    # SELECT clause
    select_parts: list[str] = []

    if query.select:
        select_parts = [_field_to_col(f) for f in query.select]
    else:
        select_parts = ["*"]

    # Add computed fields
    for expr in compute_exprs:
        select_parts.append(expr)

    select_clause = ", ".join(select_parts)

    # Use a CTE to add computed columns so they can be used in WHERE
    if compute_exprs:
        cte_select = ["*"] + compute_exprs
        sql = f"WITH computed AS (SELECT {', '.join(cte_select)} FROM src) "
        sql += f"SELECT {select_clause} FROM computed"
        # Rewrite select to use computed table (computed fields already there)
        if query.select:
            cols = [_field_to_col(f) for f in query.select]
            # Include computed field names
            for c in query.compute:
                cols.append(f'"{c.name}"')
            sql = f"WITH computed AS (SELECT {', '.join(['*'] + compute_exprs)} FROM src) "
            sql += f"SELECT {', '.join(cols)} FROM computed"
        else:
            sql = f"WITH computed AS (SELECT {', '.join(['*'] + compute_exprs)} FROM src) "
            sql += "SELECT * FROM computed"
    else:
        sql = f"SELECT {select_clause} FROM src"

    table_ref = "computed" if compute_exprs else "src"

    # WHERE clause
    if query.filter:
        where = _build_where(query.filter)
        if where:
            if compute_exprs:
                # Already using CTE, append WHERE
                sql += f" WHERE {where}"
            else:
                sql += f" WHERE {where}"

    # ORDER BY
    if query.sort:
        direction = "ASC" if query.sort.direction == "asc" else "DESC"
        sql += f" ORDER BY {_field_to_col(query.sort.field)} {direction} NULLS LAST"

    # OFFSET
    if query.offset:
        sql += f" OFFSET {query.offset}"

    # LIMIT
    limit = min(query.limit or max_output_records, max_output_records)
    sql += f" LIMIT {limit}"

    return sql


def _build_count_sql(
    query: DataQuery,
    compute_exprs: list[str],
) -> str:
    """Build a COUNT query to get total_after_filter."""
    if compute_exprs:
        sql = f"WITH computed AS (SELECT *, {', '.join(compute_exprs)} FROM src) "
        sql += "SELECT COUNT(*) AS cnt FROM computed"
    else:
        sql = "SELECT COUNT(*) AS cnt FROM src"

    if query.filter:
        where = _build_where(query.filter)
        if where:
            sql += f" WHERE {where}"

    return sql


def _build_where(filter_group: FilterGroup) -> str:
    """Build a WHERE clause from a FilterGroup (recursive)."""
    parts: list[str] = []

    for condition in filter_group.conditions:
        if isinstance(condition, FilterGroup):
            sub = _build_where(condition)
            if sub:
                parts.append(f"({sub})")
        else:
            clause = _build_condition(condition)
            if clause:
                parts.append(clause)

    if not parts:
        return ""

    joiner = " AND " if filter_group.logic == "and" else " OR "
    return joiner.join(parts)


def _build_condition(condition: FilterCondition) -> str:
    """Build a single SQL condition from a FilterCondition."""
    col = _field_to_col(condition.field)
    op = condition.operator
    value = condition.value

    if op == FilterOperator.EQ:
        return f"{col} = {_sql_value(value)}"
    elif op == FilterOperator.NE:
        return f"{col} != {_sql_value(value)}"
    elif op == FilterOperator.GT:
        return f"{col} > {_sql_value(value)}"
    elif op == FilterOperator.GTE:
        return f"{col} >= {_sql_value(value)}"
    elif op == FilterOperator.LT:
        return f"{col} < {_sql_value(value)}"
    elif op == FilterOperator.LTE:
        return f"{col} <= {_sql_value(value)}"
    elif op == FilterOperator.CONTAINS:
        escaped = str(value).replace("'", "''")
        return f"{col} LIKE '%{escaped}%'"
    elif op == FilterOperator.STARTS_WITH:
        escaped = str(value).replace("'", "''")
        return f"{col} LIKE '{escaped}%'"
    elif op == FilterOperator.ENDS_WITH:
        escaped = str(value).replace("'", "''")
        return f"{col} LIKE '%{escaped}'"
    elif op == FilterOperator.MATCHES:
        escaped = str(value).replace("'", "''")
        return f"regexp_matches({col}, '{escaped}')"
    elif op == FilterOperator.IN:
        vals = value if isinstance(value, list) else [value]
        in_list = ", ".join(_sql_value(v) for v in vals)
        return f"{col} IN ({in_list})"
    elif op == FilterOperator.NOT_IN:
        vals = value if isinstance(value, list) else [value]
        in_list = ", ".join(_sql_value(v) for v in vals)
        return f"{col} NOT IN ({in_list})"
    elif op == FilterOperator.IS_NULL:
        return f"{col} IS NULL"
    elif op == FilterOperator.IS_NOT_NULL:
        return f"{col} IS NOT NULL"
    else:
        logger.warning(f"Unknown operator: {op}")
        return ""


def _sql_value(value: Any) -> str:
    """Convert a Python value to a SQL literal."""
    if value is None:
        return "NULL"
    elif isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    elif isinstance(value, (int, float)):
        return str(value)
    elif isinstance(value, str):
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    else:
        escaped = str(value).replace("'", "''")
        return f"'{escaped}'"


# ---------------------------------------------------------------------------
# Aggregates
# ---------------------------------------------------------------------------

_AGG_FUNC_MAP: dict[str, str] = {
    AggregateFunction.COUNT: "COUNT",
    AggregateFunction.SUM: "SUM",
    AggregateFunction.AVG: "AVG",
    AggregateFunction.MIN: "MIN",
    AggregateFunction.MAX: "MAX",
    AggregateFunction.FIRST: "FIRST",
    AggregateFunction.LAST: "LAST",
    AggregateFunction.MEDIAN: "MEDIAN",
    AggregateFunction.STD: "STDDEV_SAMP",
    AggregateFunction.VAR: "VAR_SAMP",
}


def _compute_aggregates(
    engine: QueryEngine,
    aggregates: list[AggregateSpec],
    query: DataQuery,
    compute_exprs: list[str],
) -> dict[str, Any]:
    """Compute aggregate values using SQL."""
    results: dict[str, Any] = {}

    for agg in aggregates:
        func_name = agg.function
        field = agg.field

        # Handle COLLECT and DISTINCT specially
        if func_name == AggregateFunction.COLLECT:
            results[agg.name] = _collect_aggregate(engine, field, query, compute_exprs)
            continue
        elif func_name == AggregateFunction.DISTINCT:
            results[agg.name] = _distinct_aggregate(
                engine, field, query, compute_exprs
            )
            continue

        sql_func = _AGG_FUNC_MAP.get(func_name)
        if not sql_func:
            logger.warning(f"Unknown aggregate function: {func_name}")
            continue

        col = "*" if field == "*" else _field_to_col(field)
        agg_expr = f"{sql_func}({col}) AS agg_val"

        if compute_exprs:
            sql = f"WITH computed AS (SELECT *, {', '.join(compute_exprs)} FROM src) "
        else:
            sql = ""

        table = "computed" if compute_exprs else "src"

        if query.group_by:
            group_cols = [_field_to_col(g) for g in query.group_by]
            group_str = ", ".join(group_cols)
            sql += f"SELECT {group_str}, {agg_expr} FROM {table}"
            if query.filter:
                where = _build_where(query.filter)
                if where:
                    sql += f" WHERE {where}"
            sql += f" GROUP BY {group_str}"
            try:
                rows = engine.query(sql)
                # Return as dict keyed by group values
                grouped: dict[str, Any] = {}
                for row in rows:
                    key_parts = [
                        str(row[g.strip('"')])
                        for g in group_cols
                    ]
                    key = ", ".join(key_parts) if len(key_parts) > 1 else key_parts[0]
                    val = row["agg_val"]
                    grouped[key] = _to_python_number(val)
                results[agg.name] = grouped
            except Exception as e:
                logger.warning(f"Grouped aggregate failed: {e}")
                results[agg.name] = None
        else:
            sql += f"SELECT {agg_expr} FROM {table}"
            if query.filter:
                where = _build_where(query.filter)
                if where:
                    sql += f" WHERE {where}"
            try:
                rows = engine.query(sql)
                val = rows[0]["agg_val"] if rows else None
                results[agg.name] = _to_python_number(val)
            except Exception as e:
                logger.warning(f"Aggregate computation failed: {e}")
                results[agg.name] = None

    return results


def _collect_aggregate(
    engine: QueryEngine,
    field: str,
    query: DataQuery,
    compute_exprs: list[str],
) -> list[Any]:
    """Collect all values of a field into a list."""
    col = _field_to_col(field)

    if compute_exprs:
        sql = f"WITH computed AS (SELECT *, {', '.join(compute_exprs)} FROM src) "
    else:
        sql = ""

    table = "computed" if compute_exprs else "src"
    sql += f"SELECT {col} AS val FROM {table}"
    if query.filter:
        where = _build_where(query.filter)
        if where:
            sql += f" WHERE {where}"

    try:
        rows = engine.query(sql)
        return [row["val"] for row in rows]
    except Exception as e:
        logger.warning(f"Collect aggregate failed: {e}")
        return []


def _distinct_aggregate(
    engine: QueryEngine,
    field: str,
    query: DataQuery,
    compute_exprs: list[str],
) -> list[Any]:
    """Get distinct values of a field."""
    col = _field_to_col(field)

    if compute_exprs:
        sql = f"WITH computed AS (SELECT *, {', '.join(compute_exprs)} FROM src) "
    else:
        sql = ""

    table = "computed" if compute_exprs else "src"
    sql += f"SELECT DISTINCT {col} AS val FROM {table}"
    if query.filter:
        where = _build_where(query.filter)
        if where:
            sql += f" WHERE {where}"

    try:
        rows = engine.query(sql)
        return [row["val"] for row in rows]
    except Exception as e:
        logger.warning(f"Distinct aggregate failed: {e}")
        return []


def _to_python_number(val: Any) -> Any:
    """Convert DuckDB numeric types to Python float/int."""
    if val is None:
        return None
    if isinstance(val, int):
        return val
    try:
        f = float(val)
        return int(f) if f == int(f) and isinstance(val, (int, float)) else f
    except (ValueError, TypeError):
        return val
