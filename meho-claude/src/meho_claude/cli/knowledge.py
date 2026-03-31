"""Knowledge subcommand group - document ingestion and search.

Commands:
    ingest      Ingest a file into the knowledge base
    search      Search the knowledge base
    remove      Remove a knowledge source
    rebuild     Re-embed all knowledge chunks in ChromaDB
    stats       Show knowledge base statistics
"""

from __future__ import annotations

import time
from pathlib import Path

import typer

from meho_claude.cli.output import output_error, output_response

app = typer.Typer(help="Manage knowledge base.")


# ---------------------------------------------------------------------------
# Internal helpers (test-mockable seams)
# ---------------------------------------------------------------------------


def _get_settings(ctx: typer.Context):
    """Get MehoSettings from Typer context."""
    from meho_claude.cli.main import _ensure_initialized

    _ensure_initialized(ctx)
    return ctx.obj.settings


def _ingest_file(file_path: Path):
    """Ingest a file and return chunks (test-mockable seam)."""
    from meho_claude.core.knowledge.ingestor import ingest_file

    return ingest_file(file_path)


def _get_knowledge_store(state_dir: Path):
    """Get a KnowledgeStore instance (test-mockable seam)."""
    from meho_claude.core.knowledge.store import KnowledgeStore

    return KnowledgeStore(state_dir)


def _knowledge_search(conn, state_dir, query, limit=10, connector_name=None):
    """Execute knowledge hybrid search (test-mockable seam)."""
    from meho_claude.core.knowledge.search import knowledge_hybrid_search

    return knowledge_hybrid_search(conn, state_dir, query, limit=limit, connector_name=connector_name)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def ingest(
    ctx: typer.Context,
    file: Path = typer.Argument(..., help="Path to file to ingest (PDF, HTML, Markdown)."),
    connector: str | None = typer.Option(None, "--connector", help="Connector scope."),
) -> None:
    """Ingest a document into the knowledge base."""
    import hashlib

    start_time = time.time()
    settings = _get_settings(ctx)

    if not file.exists():
        output_error(
            f"File not found: {file}",
            code="FILE_NOT_FOUND",
            suggestion="Check the file path and try again.",
        )
        return

    # Compute file hash
    file_hash = hashlib.sha256(file.read_bytes()).hexdigest()

    # Ingest file to chunks
    chunks = _ingest_file(file)

    # Store chunks
    store = _get_knowledge_store(settings.state_dir)
    try:
        count = store.store_chunks(file.name, connector, chunks, file_hash)
    finally:
        store.close()

    output_response(
        {
            "status": "ok",
            "filename": file.name,
            "connector": connector,
            "chunk_count": count,
            "file_hash": file_hash,
        },
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
    """Search the knowledge base."""
    start_time = time.time()
    settings = _get_settings(ctx)

    from meho_claude.core.database import get_connection

    conn = get_connection(settings.state_dir / "meho.db")
    try:
        results = _knowledge_search(conn, settings.state_dir, query, limit=limit, connector_name=connector)
    finally:
        conn.close()

    output_response(
        {"status": "ok", "query": query, "results": results},
        human=ctx.obj.human,
        start_time=start_time,
    )


@app.command()
def remove(
    ctx: typer.Context,
    filename: str = typer.Argument(..., help="Filename of the source to remove."),
    connector: str | None = typer.Option(None, "--connector", help="Connector scope."),
) -> None:
    """Remove a knowledge source by filename."""
    start_time = time.time()
    settings = _get_settings(ctx)

    store = _get_knowledge_store(settings.state_dir)
    try:
        removed = store.remove_source(filename, connector)
    finally:
        store.close()

    if not removed:
        output_error(
            f"Knowledge source not found: {filename}",
            code="NOT_FOUND",
            suggestion="Run 'meho knowledge stats' to see ingested sources.",
        )
        return

    output_response(
        {"status": "removed", "filename": filename, "connector": connector},
        human=ctx.obj.human,
        start_time=start_time,
    )


@app.command()
def rebuild(ctx: typer.Context) -> None:
    """Re-embed all knowledge chunks in ChromaDB."""
    start_time = time.time()
    settings = _get_settings(ctx)

    store = _get_knowledge_store(settings.state_dir)
    try:
        count = store.rebuild()
    finally:
        store.close()

    output_response(
        {"status": "ok", "chunks_reindexed": count},
        human=ctx.obj.human,
        start_time=start_time,
    )


@app.command()
def stats(ctx: typer.Context) -> None:
    """Show knowledge base statistics."""
    start_time = time.time()
    settings = _get_settings(ctx)

    store = _get_knowledge_store(settings.state_dir)
    try:
        stats_data = store.get_stats()
    finally:
        store.close()

    output_response(
        {"status": "ok", **stats_data},
        human=ctx.obj.human,
        start_time=start_time,
    )
