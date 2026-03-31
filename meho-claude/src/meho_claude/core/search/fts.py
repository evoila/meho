"""FTS5 BM25 search over operations stored in SQLite.

Queries the operations_fts virtual table with weighted BM25 scoring.
Query sanitization strips FTS5 operators to prevent injection.
"""

from __future__ import annotations

import re
import sqlite3

# FTS5 operators and special characters that must be stripped
_FTS5_OPERATORS = {"AND", "OR", "NOT", "NEAR"}
_FTS5_SPECIAL_RE = re.compile(r'[:"()*^{}]')


def sanitize_fts_query(query: str) -> str:
    """Sanitize a user query for safe FTS5 MATCH usage.

    Strips FTS5 operators (AND, OR, NOT, NEAR), removes special characters
    (colons, quotes, parens, asterisks), wraps each remaining token in
    double quotes, and joins with space (implicit AND).

    Args:
        query: Raw user search query.

    Returns:
        Sanitized FTS5 query string. Empty string if no valid tokens remain.
    """
    if not query or not query.strip():
        return ""

    # Remove special characters
    cleaned = _FTS5_SPECIAL_RE.sub(" ", query)

    # Split into tokens, filter out FTS5 operators
    tokens = []
    for token in cleaned.split():
        if token.upper() not in _FTS5_OPERATORS and token.strip():
            tokens.append(token)

    if not tokens:
        return ""

    # Wrap each token in double quotes for exact matching
    return " ".join(f'"{t}"' for t in tokens)


def search_bm25(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 10,
    connector_name: str | None = None,
) -> list[dict]:
    """Search operations using FTS5 BM25 ranking.

    Uses weighted BM25 scoring:
      operation_id=1.0, display_name=5.0, description=3.0, tags=2.0

    Args:
        conn: SQLite connection with operations and operations_fts tables.
        query: Search query (will be sanitized).
        limit: Maximum number of results.
        connector_name: Optional filter to restrict results to one connector.

    Returns:
        List of dicts with id, connector_name, operation_id, display_name,
        description, trust_tier, bm25_score. Ordered by BM25 score (best first).
    """
    sanitized = sanitize_fts_query(query)
    if not sanitized:
        return []

    # BM25 weights: operation_id=1.0, display_name=5.0, description=3.0, tags=2.0
    sql = """
        SELECT
            o.id,
            o.connector_name,
            o.operation_id,
            o.display_name,
            o.description,
            o.trust_tier,
            bm25(operations_fts, 1.0, 5.0, 3.0, 2.0) AS bm25_score
        FROM operations_fts
        JOIN operations o ON operations_fts.rowid = o.id
        WHERE operations_fts MATCH ?
    """
    params: list = [sanitized]

    if connector_name:
        sql += " AND o.connector_name = ?"
        params.append(connector_name)

    sql += " ORDER BY bm25_score LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()

    return [
        {
            "id": row["id"],
            "connector_name": row["connector_name"],
            "operation_id": row["operation_id"],
            "display_name": row["display_name"],
            "description": row["description"],
            "trust_tier": row["trust_tier"],
            "bm25_score": row["bm25_score"],
        }
        for row in rows
    ]
