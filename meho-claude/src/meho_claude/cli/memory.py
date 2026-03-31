"""Memory subcommand group - connector-scoped memory management.

Commands:
    store       Store a memory entry
    search      Search memory entries
    list        List all memory entries
    forget      Remove a memory entry
"""

from __future__ import annotations

import time
from pathlib import Path

import typer

from meho_claude.cli.output import output_error, output_response

app = typer.Typer(help="Manage connector-scoped memory.")


# ---------------------------------------------------------------------------
# Internal helpers (test-mockable seams)
# ---------------------------------------------------------------------------


def _get_settings(ctx: typer.Context):
    """Get MehoSettings from Typer context."""
    from meho_claude.cli.main import _ensure_initialized

    _ensure_initialized(ctx)
    return ctx.obj.settings


def _get_memory_store(state_dir: Path):
    """Get a MemoryStore instance (test-mockable seam)."""
    from meho_claude.core.memory.store import MemoryStore

    return MemoryStore(state_dir)


def _memory_search(conn, state_dir, query, limit=10, connector_name=None):
    """Execute memory hybrid search (test-mockable seam)."""
    from meho_claude.core.memory.search import memory_hybrid_search

    return memory_hybrid_search(conn, state_dir, query, limit=limit, connector_name=connector_name)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def store(
    ctx: typer.Context,
    text: str = typer.Argument(..., help="Memory text to store."),
    connector: str | None = typer.Option(None, "--connector", help="Connector scope."),
    tags: str = typer.Option("", "--tags", help="Comma-separated tags."),
) -> None:
    """Store a memory entry."""
    start_time = time.time()
    settings = _get_settings(ctx)

    memory_store = _get_memory_store(settings.state_dir)
    try:
        mem = memory_store.store_memory(text, connector_name=connector, tags=tags)
    finally:
        memory_store.close()

    output_response(
        {"status": "ok", "memory": mem},
        human=ctx.obj.human,
        start_time=start_time,
    )


@app.command()
def search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Search query."),
    connector: str | None = typer.Option(None, "--connector", help="Filter by connector."),
    limit: int = typer.Option(5, "--limit", help="Maximum results."),
) -> None:
    """Search memory entries."""
    start_time = time.time()
    settings = _get_settings(ctx)

    from meho_claude.core.database import get_connection

    conn = get_connection(settings.state_dir / "meho.db")
    try:
        results = _memory_search(conn, settings.state_dir, query, limit=limit, connector_name=connector)
    finally:
        conn.close()

    output_response(
        {"status": "ok", "query": query, "results": results},
        human=ctx.obj.human,
        start_time=start_time,
    )


@app.command("list")
def list_memories(
    ctx: typer.Context,
    connector: str | None = typer.Option(None, "--connector", help="Filter by connector."),
) -> None:
    """List all memory entries."""
    start_time = time.time()
    settings = _get_settings(ctx)

    memory_store = _get_memory_store(settings.state_dir)
    try:
        memories = memory_store.list_memories(connector_name=connector)
    finally:
        memory_store.close()

    output_response(
        {"status": "ok", "memories": memories, "count": len(memories)},
        human=ctx.obj.human,
        start_time=start_time,
    )


@app.command()
def forget(
    ctx: typer.Context,
    memory_id: str = typer.Argument(..., help="Memory ID to forget."),
) -> None:
    """Remove a memory entry."""
    start_time = time.time()
    settings = _get_settings(ctx)

    memory_store = _get_memory_store(settings.state_dir)
    try:
        forgotten = memory_store.forget_memory(memory_id)
    finally:
        memory_store.close()

    if not forgotten:
        output_error(
            f"Memory not found: {memory_id}",
            code="NOT_FOUND",
            suggestion="Use 'meho memory list' to see available memory IDs.",
        )
        return

    output_response(
        {"status": "ok", "forgotten": True, "memory_id": memory_id},
        human=ctx.obj.human,
        start_time=start_time,
    )
