"""Knowledge hybrid search: BM25 + semantic + RRF fusion.

Searches knowledge chunks stored in SQLite (FTS5) and ChromaDB.
Falls back gracefully to BM25-only when ChromaDB is empty or errors.
Replicates the topology_hybrid_search pattern exactly.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from meho_claude.core.search.fts import sanitize_fts_query

if TYPE_CHECKING:
    from chromadb.api.models.Collection import Collection

logger = structlog.get_logger()


def search_knowledge_bm25(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 10,
    connector_name: str | None = None,
) -> list[dict]:
    """Search knowledge chunks using FTS5 BM25 ranking.

    Uses weighted BM25 scoring:
      content=5.0, heading=3.0

    Args:
        conn: SQLite connection with knowledge_chunks and knowledge_chunks_fts tables.
        query: Search query (will be sanitized).
        limit: Maximum number of results.
        connector_name: Optional filter to restrict results to one connector.

    Returns:
        List of dicts with id, source_id, content, heading, connector_name,
        token_estimate, source_file, bm25_score. Ordered by BM25 score (best first).
    """
    sanitized = sanitize_fts_query(query)
    if not sanitized:
        return []

    # BM25 weights: content=5.0, heading=3.0
    sql = """
        SELECT
            kc.id,
            kc.source_id,
            kc.content,
            kc.heading,
            kc.connector_name,
            kc.token_estimate,
            ks.filename AS source_file,
            bm25(knowledge_chunks_fts, 5.0, 3.0) AS bm25_score
        FROM knowledge_chunks_fts
        JOIN knowledge_chunks kc ON knowledge_chunks_fts.rowid = kc.rowid
        JOIN knowledge_sources ks ON kc.source_id = ks.id
        WHERE knowledge_chunks_fts MATCH ?
    """
    params: list = [sanitized]

    if connector_name:
        sql += " AND kc.connector_name = ?"
        params.append(connector_name)

    sql += " ORDER BY bm25_score LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()

    return [
        {
            "id": row["id"],
            "source_id": row["source_id"],
            "content": row["content"],
            "heading": row["heading"],
            "connector_name": row["connector_name"],
            "token_estimate": row["token_estimate"],
            "source_file": row["source_file"],
            "bm25_score": row["bm25_score"],
        }
        for row in rows
    ]


def get_knowledge_collection(client):
    """Get or create the 'knowledge_chunks' ChromaDB collection.

    Args:
        client: ChromaDB client.

    Returns:
        Collection configured for cosine similarity search.
    """
    return client.get_or_create_collection(
        name="knowledge_chunks",
        metadata={"hnsw:space": "cosine"},
    )


def search_knowledge_semantic(
    collection: Collection,
    query: str,
    limit: int = 20,
    connector_name: str | None = None,
) -> list[dict]:
    """Search knowledge chunks using ChromaDB semantic similarity.

    Args:
        collection: ChromaDB knowledge_chunks collection.
        query: Natural language search query.
        limit: Maximum number of results.
        connector_name: Optional connector filter.

    Returns:
        List of dicts with id, connector_name, source_id, distance.
        Ordered by distance (closest first).
    """
    if collection.count() == 0:
        return []

    kwargs: dict = {
        "query_texts": [query],
        "n_results": min(limit, collection.count()),
    }

    if connector_name:
        kwargs["where"] = {"connector_name": connector_name}

    results = collection.query(**kwargs)

    if not results["ids"] or not results["ids"][0]:
        return []

    output = []
    for i, doc_id in enumerate(results["ids"][0]):
        meta = results["metadatas"][0][i]
        distance = results["distances"][0][i] if results["distances"] else 0.0
        output.append({
            "id": doc_id,
            "connector_name": meta.get("connector_name", "__global__"),
            "source_id": meta.get("source_id", ""),
            "distance": distance,
        })

    return output


def _knowledge_rrf(
    bm25_results: list[dict],
    semantic_results: list[dict],
    k: int = 60,
) -> list[dict]:
    """Reciprocal Rank Fusion for knowledge search results.

    Same algorithm as search.hybrid.reciprocal_rank_fusion but keys on
    chunk 'id' field instead of 'connector_name:operation_id'.

    Args:
        bm25_results: Ranked results from BM25 search.
        semantic_results: Ranked results from semantic search.
        k: Smoothing constant (default 60).

    Returns:
        Merged list sorted by RRF score (highest first), with relevance_score field.
    """
    scores: dict[str, float] = {}
    items: dict[str, dict] = {}

    # Score BM25 results
    for rank, result in enumerate(bm25_results, start=1):
        key = result["id"]
        scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
        if key not in items:
            items[key] = dict(result)

    # Score semantic results
    for rank, result in enumerate(semantic_results, start=1):
        key = result["id"]
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


def knowledge_hybrid_search(
    conn: sqlite3.Connection,
    state_dir: Path,
    query: str,
    limit: int = 10,
    connector_name: str | None = None,
) -> list[dict]:
    """Execute hybrid search over knowledge chunks.

    Combines BM25 (FTS5) and semantic (ChromaDB) results via Reciprocal Rank Fusion.
    Falls back to BM25-only when ChromaDB is unavailable or empty.

    Args:
        conn: SQLite connection with knowledge_chunks and knowledge_chunks_fts tables.
        state_dir: Path to meho state directory (for ChromaDB path).
        query: Natural language search query.
        limit: Maximum number of results.
        connector_name: Optional filter to restrict results to one connector.

    Returns:
        Ranked list of chunk dicts with relevance_score field.
    """
    # Get BM25 results (always available)
    bm25_results = search_knowledge_bm25(
        conn, query, limit=limit * 2, connector_name=connector_name
    )

    # Try to get semantic results
    semantic_results: list[dict] = []
    try:
        from meho_claude.core.search.semantic import get_chroma_client

        client = get_chroma_client(state_dir)
        collection = get_knowledge_collection(client)

        if collection.count() > 0:
            semantic_results = search_knowledge_semantic(
                collection, query, limit=limit * 2, connector_name=connector_name
            )
    except Exception as exc:
        logger.warning("knowledge_chromadb_search_failed", error=str(exc))
        # Fall back to BM25-only

    # Merge via knowledge-specific RRF
    merged = _knowledge_rrf(bm25_results, semantic_results, k=60)

    return merged[:limit]
