"""Topology-specific hybrid search: FTS5 BM25 + ChromaDB semantic + RRF fusion.

Searches topology entities stored in topology.db and ChromaDB topology_entities
collection. Falls back gracefully to BM25-only when ChromaDB is empty or errors.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from meho_claude.core.search.fts import sanitize_fts_query

if TYPE_CHECKING:
    from chromadb.api.models.Collection import Collection

    from meho_claude.core.topology.models import TopologyEntity

logger = structlog.get_logger()


def search_topology_bm25(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 10,
    connector_name: str | None = None,
    entity_type: str | None = None,
) -> list[dict]:
    """Search topology entities using FTS5 BM25 ranking.

    Uses weighted BM25 scoring:
      name=5.0, entity_type=2.0, description=3.0

    Args:
        conn: SQLite connection with topology_entities and topology_entities_fts tables.
        query: Search query (will be sanitized).
        limit: Maximum number of results.
        connector_name: Optional filter to restrict results to one connector.
        entity_type: Optional filter to restrict results to one entity type.

    Returns:
        List of dicts with id, name, entity_type, connector_name, connector_type,
        canonical_id, description, bm25_score. Ordered by BM25 score (best first).
    """
    sanitized = sanitize_fts_query(query)
    if not sanitized:
        return []

    # BM25 weights: name=5.0, entity_type=2.0, description=3.0
    sql = """
        SELECT
            e.id,
            e.name,
            e.entity_type,
            e.connector_name,
            e.connector_type,
            e.canonical_id,
            e.description,
            bm25(topology_entities_fts, 5.0, 2.0, 3.0) AS bm25_score
        FROM topology_entities_fts
        JOIN topology_entities e ON topology_entities_fts.rowid = e.rowid
        WHERE topology_entities_fts MATCH ?
    """
    params: list = [sanitized]

    if connector_name:
        sql += " AND e.connector_name = ?"
        params.append(connector_name)

    if entity_type:
        sql += " AND e.entity_type = ?"
        params.append(entity_type)

    sql += " ORDER BY bm25_score LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()

    return [
        {
            "id": row["id"],
            "name": row["name"],
            "entity_type": row["entity_type"],
            "connector_name": row["connector_name"],
            "connector_type": row["connector_type"],
            "canonical_id": row["canonical_id"],
            "description": row["description"],
            "bm25_score": row["bm25_score"],
        }
        for row in rows
    ]


def get_topology_collection(client):
    """Get or create the 'topology_entities' ChromaDB collection.

    Args:
        client: ChromaDB client.

    Returns:
        Collection configured for cosine similarity search.
    """
    return client.get_or_create_collection(
        name="topology_entities",
        metadata={"hnsw:space": "cosine"},
    )


def embed_topology_entities(state_dir: Path, entities: list[TopologyEntity]) -> None:
    """Embed topology entities into ChromaDB topology_entities collection.

    Builds a composite document for each entity from its type, name, description,
    and scope entries. Uses batch upsert for efficiency. Never raises --
    all exceptions are caught and logged.

    Args:
        state_dir: Path to meho state directory (~/.meho).
        entities: List of TopologyEntity objects to embed.
    """
    try:
        from meho_claude.core.search.semantic import get_chroma_client

        client = get_chroma_client(state_dir)
        collection = get_topology_collection(client)

        ids = []
        documents = []
        metadatas = []

        for entity in entities:
            # Build composite document: entity_type name description + scope key/value pairs
            doc_parts = [entity.entity_type, entity.name, entity.description]
            for key, value in entity.scope.items():
                doc_parts.append(f"{key} {value}")
            composite_doc = " ".join(doc_parts).strip()

            ids.append(entity.id)
            documents.append(composite_doc)
            metadatas.append({
                "connector_name": entity.connector_name or "",
                "connector_type": entity.connector_type,
                "entity_type": entity.entity_type,
                "canonical_id": entity.canonical_id,
            })

        if ids:
            collection.upsert(ids=ids, documents=documents, metadatas=metadatas)

    except Exception as exc:
        logger.warning(
            "topology_embedding_failed",
            entity_count=len(entities),
            error=str(exc),
        )


def search_topology_semantic(
    collection: Collection,
    query: str,
    limit: int = 20,
) -> list[dict]:
    """Search topology entities using ChromaDB semantic similarity.

    Args:
        collection: ChromaDB topology_entities collection.
        query: Natural language search query.
        limit: Maximum number of results.

    Returns:
        List of dicts with id, connector_name, connector_type, entity_type,
        canonical_id, distance. Ordered by distance (closest first).
    """
    if collection.count() == 0:
        return []

    results = collection.query(
        query_texts=[query],
        n_results=min(limit, collection.count()),
    )

    if not results["ids"] or not results["ids"][0]:
        return []

    output = []
    for i, doc_id in enumerate(results["ids"][0]):
        meta = results["metadatas"][0][i]
        distance = results["distances"][0][i] if results["distances"] else 0.0
        output.append({
            "id": doc_id,
            "connector_name": meta.get("connector_name", ""),
            "connector_type": meta.get("connector_type", ""),
            "entity_type": meta.get("entity_type", ""),
            "canonical_id": meta.get("canonical_id", ""),
            "distance": distance,
        })

    return output


def _topology_rrf(
    bm25_results: list[dict],
    semantic_results: list[dict],
    k: int = 60,
) -> list[dict]:
    """Reciprocal Rank Fusion for topology search results.

    Same algorithm as search.hybrid.reciprocal_rank_fusion but keys on
    entity 'id' field instead of 'connector_name:operation_id'.

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


def topology_hybrid_search(
    conn: sqlite3.Connection,
    state_dir: Path,
    query: str,
    limit: int = 10,
    connector_name: str | None = None,
    entity_type: str | None = None,
) -> list[dict]:
    """Execute hybrid search over topology entities.

    Combines BM25 (FTS5) and semantic (ChromaDB) results via Reciprocal Rank Fusion.
    Falls back to BM25-only when ChromaDB is unavailable or empty.

    Args:
        conn: SQLite connection with topology_entities and topology_entities_fts tables.
        state_dir: Path to meho state directory (for ChromaDB path).
        query: Natural language search query.
        limit: Maximum number of results.
        connector_name: Optional filter to restrict results to one connector.
        entity_type: Optional filter to restrict results to one entity type.

    Returns:
        Ranked list of entity dicts with relevance_score field.
    """
    # Get BM25 results (always available)
    bm25_results = search_topology_bm25(
        conn, query, limit=limit * 2, connector_name=connector_name, entity_type=entity_type
    )

    # Try to get semantic results
    semantic_results: list[dict] = []
    try:
        from meho_claude.core.search.semantic import get_chroma_client

        client = get_chroma_client(state_dir)
        collection = get_topology_collection(client)

        if collection.count() > 0:
            semantic_results = search_topology_semantic(collection, query, limit=limit * 2)

            # Apply filters to semantic results
            if connector_name and semantic_results:
                semantic_results = [
                    r for r in semantic_results if r["connector_name"] == connector_name
                ]
            if entity_type and semantic_results:
                semantic_results = [
                    r for r in semantic_results if r["entity_type"] == entity_type
                ]
    except Exception as exc:
        logger.warning("topology_chromadb_search_failed", error=str(exc))
        # Fall back to BM25-only

    # Merge via topology-specific RRF
    merged = _topology_rrf(bm25_results, semantic_results, k=60)

    return merged[:limit]
