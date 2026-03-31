"""Hybrid search: BM25 + ChromaDB semantic merged via Reciprocal Rank Fusion.

Falls back to BM25-only when ChromaDB is unavailable or empty.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import structlog

from meho_claude.core.search.fts import search_bm25

logger = structlog.get_logger()


def reciprocal_rank_fusion(
    bm25_results: list[dict],
    semantic_results: list[dict],
    k: int = 60,
) -> list[dict]:
    """Merge two ranked result lists using Reciprocal Rank Fusion.

    RRF score = sum(1 / (k + rank)) for each list where the item appears.
    Items appearing in both lists get boosted.

    Args:
        bm25_results: Ranked results from BM25 search.
        semantic_results: Ranked results from semantic search.
        k: Smoothing constant (default 60, standard in literature).

    Returns:
        Merged list sorted by RRF score (highest first), with relevance_score field.
    """
    scores: dict[str, float] = {}
    items: dict[str, dict] = {}

    # Score BM25 results
    for rank, result in enumerate(bm25_results, start=1):
        key = f"{result['connector_name']}:{result['operation_id']}"
        scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
        if key not in items:
            items[key] = dict(result)

    # Score semantic results
    for rank, result in enumerate(semantic_results, start=1):
        key = f"{result['connector_name']}:{result['operation_id']}"
        scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
        if key not in items:
            items[key] = dict(result)

    # Build merged results with relevance_score
    merged = []
    for key, score in scores.items():
        item = items[key]
        item["relevance_score"] = round(score, 6)
        # Remove source-specific scoring fields
        item.pop("bm25_score", None)
        item.pop("distance", None)
        merged.append(item)

    # Sort by RRF score descending
    merged.sort(key=lambda x: x["relevance_score"], reverse=True)

    return merged


def hybrid_search(
    conn: sqlite3.Connection,
    state_dir: Path,
    query: str,
    limit: int = 10,
    connector_name: str | None = None,
) -> list[dict]:
    """Execute hybrid search combining BM25 and semantic results.

    Falls back to BM25-only results when ChromaDB is unavailable or empty.

    Args:
        conn: SQLite connection with operations and operations_fts tables.
        state_dir: Path to meho state directory (for ChromaDB path).
        query: Natural language search query.
        limit: Maximum number of results.
        connector_name: Optional filter to restrict results to one connector.

    Returns:
        Ranked list of operation dicts with relevance_score field.
    """
    # Get BM25 results (always available)
    bm25_results = search_bm25(conn, query, limit=limit * 2, connector_name=connector_name)

    # Try to get semantic results
    semantic_results: list[dict] = []
    try:
        from meho_claude.core.search.semantic import (
            get_chroma_client,
            get_operations_collection,
            search_semantic,
        )

        client = get_chroma_client(state_dir)
        collection = get_operations_collection(client)

        if collection.count() > 0:
            semantic_results = search_semantic(collection, query, limit=limit * 2)

            # Filter by connector_name if specified
            if connector_name and semantic_results:
                semantic_results = [
                    r for r in semantic_results if r["connector_name"] == connector_name
                ]
    except Exception as exc:
        logger.warning("chromadb_search_failed", error=str(exc))
        # Fall back to BM25-only

    # Merge via RRF
    merged = reciprocal_rank_fusion(bm25_results, semantic_results, k=60)

    return merged[:limit]
