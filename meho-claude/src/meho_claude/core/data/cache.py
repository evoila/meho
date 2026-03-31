"""DuckDB response cache with auto-inferred schemas.

Caches large API responses as DuckDB tables for SQL querying.
Uses DuckDB's read_json_auto() for automatic schema inference from JSON.
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any

import duckdb


def _sanitize_table_name(connector_name: str, operation_id: str) -> str:
    """Build a sanitized DuckDB table name from connector and operation.

    Replaces hyphens, dots, and spaces with underscores.

    Args:
        connector_name: Connector name.
        operation_id: Operation identifier.

    Returns:
        Sanitized table name like "k8s_prod_listPods".
    """
    raw = f"{connector_name}_{operation_id}"
    return re.sub(r"[-.\s]+", "_", raw)


class ResponseCache:
    """DuckDB-backed cache for API response data.

    Stores large JSON responses as DuckDB tables with auto-inferred schemas.
    Supports SQL querying with pagination.

    Args:
        db_path: Path to the DuckDB database file.
        size_threshold: Minimum JSON size in bytes to trigger caching (default 4096).
    """

    def __init__(self, db_path: Path, size_threshold: int = 4096) -> None:
        self._db_path = db_path
        self._size_threshold = size_threshold
        self._conn = duckdb.connect(str(db_path))

    def should_cache(self, response_data: Any) -> bool:
        """Check if a response should be cached based on serialized size.

        Args:
            response_data: The API response data (typically a list of dicts).

        Returns:
            True if JSON-serialized size exceeds the threshold.
        """
        try:
            serialized = json.dumps(response_data)
            return len(serialized) > self._size_threshold
        except (TypeError, ValueError):
            return False

    def cache_response(
        self,
        connector_name: str,
        operation_id: str,
        data: list[dict],
    ) -> dict:
        """Cache a response as a DuckDB table with auto-inferred schema.

        Writes data to a temp JSON file, uses DuckDB read_json_auto() for
        schema inference, creates/replaces the table.

        Args:
            connector_name: Connector name.
            operation_id: Operation identifier.
            data: List of dicts (JSON-serializable rows).

        Returns:
            Summary dict with status, table, row_count, columns, sample, query_hint.
        """
        table_name = _sanitize_table_name(connector_name, operation_id)

        # Write to temp JSON file for DuckDB to read
        tmp_file = None
        try:
            tmp_file = tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False
            )
            json.dump(data, tmp_file)
            tmp_file.close()

            # Create or replace table from JSON
            self._conn.execute(
                f"CREATE OR REPLACE TABLE {table_name} AS "
                f"SELECT * FROM read_json_auto('{tmp_file.name}')"
            )

            # Get metadata
            row_count = self._conn.execute(
                f"SELECT COUNT(*) FROM {table_name}"
            ).fetchone()[0]

            columns_result = self._conn.execute(
                f"SELECT column_name FROM information_schema.columns "
                f"WHERE table_name = '{table_name}' ORDER BY ordinal_position"
            ).fetchall()
            columns = [row[0] for row in columns_result]

            # Get sample rows (first 3)
            sample_rows = self._conn.execute(
                f"SELECT * FROM {table_name} LIMIT 3"
            ).fetchall()
            sample_columns = [desc[0] for desc in self._conn.description]
            sample = [dict(zip(sample_columns, row)) for row in sample_rows]

            return {
                "status": "cached",
                "table": table_name,
                "row_count": row_count,
                "columns": columns,
                "sample": sample,
                "query_hint": f"meho data query 'SELECT * FROM {table_name} WHERE ...'",
            }

        finally:
            if tmp_file:
                Path(tmp_file.name).unlink(missing_ok=True)

    def query(self, sql: str, limit: int = 100, offset: int = 0) -> dict:
        """Execute a SQL query over cached data.

        Injects LIMIT and OFFSET if not already present in the SQL.

        Args:
            sql: SQL query string.
            limit: Maximum rows to return (default 100).
            offset: Row offset for pagination (default 0).

        Returns:
            Dict with columns, rows (list of dicts), and row_count.
        """
        # Inject LIMIT/OFFSET if not present
        sql_upper = sql.upper().strip()
        if "LIMIT" not in sql_upper:
            sql = f"{sql.rstrip().rstrip(';')} LIMIT {limit} OFFSET {offset}"

        result = self._conn.execute(sql)
        columns = [desc[0] for desc in result.description]
        rows_raw = result.fetchall()
        rows = [dict(zip(columns, row)) for row in rows_raw]

        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
        }

    def list_tables(self) -> list[str]:
        """List all cached table names.

        Returns:
            Sorted list of table names in the cache.
        """
        result = self._conn.execute("SHOW TABLES").fetchall()
        return sorted(row[0] for row in result)

    def close(self) -> None:
        """Close the DuckDB connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
