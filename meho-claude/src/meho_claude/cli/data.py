"""Data subcommand group - SQL queries over cached API responses.

Commands:
    query   Run SQL queries over DuckDB-cached API response data
"""

from __future__ import annotations

import time
from typing import Optional

import typer

from meho_claude.cli.output import output_error, output_response

app = typer.Typer(help="Query cached data with SQL.")


def _get_settings(ctx: typer.Context):
    """Get MehoSettings from Typer context."""
    from meho_claude.cli.main import _ensure_initialized

    _ensure_initialized(ctx)
    return ctx.obj.settings


@app.command()
def query(
    ctx: typer.Context,
    sql: str = typer.Argument(..., help="SQL query to execute."),
    limit: int = typer.Option(100, "--limit", help="Maximum rows to return."),
    offset: int = typer.Option(0, "--offset", help="Row offset for pagination."),
) -> None:
    """Run SQL queries over cached API response data."""
    start_time = time.time()
    settings = _get_settings(ctx)
    state_dir = settings.state_dir

    from meho_claude.core.data.cache import ResponseCache

    cache = ResponseCache(state_dir / "cache.duckdb")
    try:
        result = cache.query(sql, limit=limit, offset=offset)
        output_response(
            {
                "status": "ok",
                "columns": result["columns"],
                "rows": result["rows"],
                "row_count": result["row_count"],
            },
            human=ctx.obj.human,
            start_time=start_time,
        )
    except Exception as exc:
        output_error(
            f"Query failed: {exc}",
            code="QUERY_ERROR",
            suggestion="Run 'meho data query \"SHOW TABLES\"' to see available tables.",
        )
    finally:
        cache.close()
