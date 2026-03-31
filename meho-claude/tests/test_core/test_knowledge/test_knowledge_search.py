"""Tests for knowledge hybrid search (BM25 + semantic + RRF)."""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from meho_claude.core.knowledge.search import (
    knowledge_hybrid_search,
    search_knowledge_bm25,
)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply knowledge migration SQL to an in-memory connection."""
    from importlib import resources

    for name in ("001_initial.sql", "002_operations.sql", "003_knowledge.sql", "004_memory.sql"):
        sql = resources.files("meho_claude.db.migrations.meho").joinpath(name).read_text()
        conn.executescript(sql)


def _insert_test_chunks(
    conn: sqlite3.Connection,
    filename: str = "doc.md",
    connector_name: str | None = "test-conn",
    chunks: list[tuple[str, str]] | None = None,
) -> str:
    """Insert test chunks directly into SQLite for search testing.

    Args:
        conn: SQLite connection.
        filename: Source filename.
        connector_name: Connector name (None for global).
        chunks: List of (content, heading) tuples.

    Returns:
        Source ID.
    """
    if chunks is None:
        chunks = [
            ("Kubernetes pod crashloopbackoff troubleshooting guide", "# K8s Pods"),
            ("VMware vSphere host memory high utilization", "## VMware Memory"),
            ("Network latency diagnosis between microservices", "# Network"),
        ]

    source_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO knowledge_sources (id, filename, connector_name, file_hash, chunk_count) "
        "VALUES (?, ?, ?, ?, ?)",
        (source_id, filename, connector_name, "testhash", len(chunks)),
    )

    for i, (content, heading) in enumerate(chunks):
        chunk_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO knowledge_chunks "
            "(id, source_id, chunk_index, content, heading, token_estimate, connector_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chunk_id, source_id, i, content, heading, 10, connector_name),
        )

    conn.commit()
    return source_id


@pytest.fixture()
def search_conn(tmp_path: Path) -> sqlite3.Connection:
    """Create an in-memory SQLite connection with migrations applied."""
    db_path = tmp_path / "meho.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_migrations(conn)
    return conn


class TestSearchKnowledgeBM25:
    """Tests for search_knowledge_bm25 function."""

    def test_returns_matching_chunks(self, search_conn: sqlite3.Connection):
        """search_knowledge_bm25 returns matching chunks ranked by BM25."""
        _insert_test_chunks(search_conn)
        results = search_knowledge_bm25(search_conn, "kubernetes pod")
        assert len(results) >= 1
        assert "kubernetes" in results[0]["content"].lower() or "pod" in results[0]["content"].lower()
        assert "bm25_score" in results[0]
        assert "source_file" in results[0]

    def test_filters_by_connector(self, search_conn: sqlite3.Connection):
        """search_knowledge_bm25 with connector_name filters results."""
        _insert_test_chunks(search_conn, connector_name="conn-a")
        _insert_test_chunks(
            search_conn,
            filename="other.md",
            connector_name="conn-b",
            chunks=[("Kubernetes advanced scheduling", "# Advanced")],
        )

        results = search_knowledge_bm25(search_conn, "kubernetes", connector_name="conn-a")
        assert len(results) >= 1
        assert all(r["connector_name"] == "conn-a" for r in results)

    def test_empty_query_returns_empty(self, search_conn: sqlite3.Connection):
        """search_knowledge_bm25 with empty query returns []."""
        _insert_test_chunks(search_conn)
        results = search_knowledge_bm25(search_conn, "")
        assert results == []

    def test_no_match_returns_empty(self, search_conn: sqlite3.Connection):
        """search_knowledge_bm25 with no matching terms returns []."""
        _insert_test_chunks(search_conn)
        results = search_knowledge_bm25(search_conn, "xyznonexistent123")
        assert results == []


class TestKnowledgeHybridSearch:
    """Tests for knowledge_hybrid_search function."""

    def test_returns_results_with_relevance_score(self, search_conn: sqlite3.Connection, tmp_path: Path):
        """knowledge_hybrid_search returns results with relevance_score."""
        _insert_test_chunks(search_conn)
        results = knowledge_hybrid_search(
            search_conn, tmp_path, "kubernetes pod", limit=5
        )
        assert len(results) >= 1
        assert "relevance_score" in results[0]

    def test_falls_back_to_bm25_when_chromadb_empty(self, search_conn: sqlite3.Connection, tmp_path: Path):
        """knowledge_hybrid_search falls back to BM25 when ChromaDB empty."""
        _insert_test_chunks(search_conn)

        # Mock ChromaDB with empty collection
        mock_client = MagicMock()
        mock_collection = MagicMock()
        mock_collection.count.return_value = 0
        mock_client.get_or_create_collection.return_value = mock_collection

        with patch(
            "meho_claude.core.search.semantic.get_chroma_client",
            return_value=mock_client,
        ):
            results = knowledge_hybrid_search(
                search_conn, tmp_path, "kubernetes", limit=5
            )

        assert len(results) >= 1
        assert "relevance_score" in results[0]

    def test_limits_results(self, search_conn: sqlite3.Connection, tmp_path: Path):
        """knowledge_hybrid_search respects limit parameter."""
        _insert_test_chunks(search_conn)
        results = knowledge_hybrid_search(
            search_conn, tmp_path, "troubleshooting", limit=1
        )
        assert len(results) <= 1

    def test_connector_filter(self, search_conn: sqlite3.Connection, tmp_path: Path):
        """knowledge_hybrid_search filters by connector_name."""
        _insert_test_chunks(search_conn, connector_name="conn-a")
        _insert_test_chunks(
            search_conn,
            filename="other.md",
            connector_name="conn-b",
            chunks=[("Kubernetes monitoring setup", "# Monitoring")],
        )
        results = knowledge_hybrid_search(
            search_conn, tmp_path, "kubernetes", limit=10, connector_name="conn-a"
        )
        assert all(r["connector_name"] == "conn-a" for r in results)
