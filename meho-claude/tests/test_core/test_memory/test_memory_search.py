"""Tests for memory hybrid search (BM25 + semantic + RRF)."""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from meho_claude.core.memory.search import (
    memory_hybrid_search,
    search_memory_bm25,
)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply all meho migrations to a connection."""
    from importlib import resources

    for name in ("001_initial.sql", "002_operations.sql", "003_knowledge.sql", "004_memory.sql"):
        sql = resources.files("meho_claude.db.migrations.meho").joinpath(name).read_text()
        conn.executescript(sql)


def _insert_test_memories(
    conn: sqlite3.Connection,
    memories: list[tuple[str, str | None, str]] | None = None,
) -> list[str]:
    """Insert test memories directly into SQLite for search testing.

    Args:
        conn: SQLite connection.
        memories: List of (content, connector_name, tags) tuples.

    Returns:
        List of memory IDs.
    """
    if memories is None:
        memories = [
            ("OOM kills on pod X were caused by memory limit too low", "k8s", "pattern,resolution"),
            ("VMware host ESXi01 had DRS migration storm due to affinity rule conflict", "vmware", "issue"),
            ("Network latency between service-a and service-b was caused by DNS timeout", None, "network,dns"),
        ]

    ids = []
    for content, connector_name, tags in memories:
        memory_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO memories (id, content, connector_name, tags) VALUES (?, ?, ?, ?)",
            (memory_id, content, connector_name, tags),
        )
        ids.append(memory_id)

    conn.commit()
    return ids


@pytest.fixture()
def search_conn(tmp_path: Path) -> sqlite3.Connection:
    """Create a SQLite connection with migrations applied."""
    db_path = tmp_path / "meho.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_migrations(conn)
    return conn


class TestSearchMemoryBM25:
    """Tests for search_memory_bm25 function."""

    def test_returns_matching_memories(self, search_conn: sqlite3.Connection):
        """search_memory_bm25 returns matching memories ranked by BM25."""
        _insert_test_memories(search_conn)
        results = search_memory_bm25(search_conn, "OOM pod memory")
        assert len(results) >= 1
        assert "bm25_score" in results[0]
        assert "content" in results[0]

    def test_filters_by_connector(self, search_conn: sqlite3.Connection):
        """search_memory_bm25 with connector_name filters results."""
        _insert_test_memories(search_conn)
        results = search_memory_bm25(search_conn, "OOM pod memory", connector_name="k8s")
        assert len(results) >= 1
        assert all(r["connector_name"] == "k8s" for r in results)

    def test_empty_query_returns_empty(self, search_conn: sqlite3.Connection):
        """search_memory_bm25 with empty query returns []."""
        _insert_test_memories(search_conn)
        results = search_memory_bm25(search_conn, "")
        assert results == []

    def test_no_match_returns_empty(self, search_conn: sqlite3.Connection):
        """search_memory_bm25 with no matching terms returns []."""
        _insert_test_memories(search_conn)
        results = search_memory_bm25(search_conn, "xyznonexistent123")
        assert results == []


class TestMemoryHybridSearch:
    """Tests for memory_hybrid_search function."""

    def test_returns_results_with_relevance_score(self, search_conn: sqlite3.Connection, tmp_path: Path):
        """memory_hybrid_search returns results with relevance_score."""
        _insert_test_memories(search_conn)
        results = memory_hybrid_search(
            search_conn, tmp_path, "OOM pod memory", limit=5
        )
        assert len(results) >= 1
        assert "relevance_score" in results[0]

    def test_falls_back_to_bm25_when_chromadb_empty(self, search_conn: sqlite3.Connection, tmp_path: Path):
        """memory_hybrid_search falls back to BM25 when ChromaDB empty."""
        _insert_test_memories(search_conn)

        mock_client = MagicMock()
        mock_collection = MagicMock()
        mock_collection.count.return_value = 0
        mock_client.get_or_create_collection.return_value = mock_collection

        with patch(
            "meho_claude.core.search.semantic.get_chroma_client",
            return_value=mock_client,
        ):
            results = memory_hybrid_search(
                search_conn, tmp_path, "OOM kill", limit=5
            )

        assert len(results) >= 1
        assert "relevance_score" in results[0]

    def test_limits_results(self, search_conn: sqlite3.Connection, tmp_path: Path):
        """memory_hybrid_search respects limit parameter."""
        _insert_test_memories(search_conn)
        results = memory_hybrid_search(
            search_conn, tmp_path, "memory", limit=1
        )
        assert len(results) <= 1

    def test_connector_filter(self, search_conn: sqlite3.Connection, tmp_path: Path):
        """memory_hybrid_search filters by connector_name."""
        _insert_test_memories(search_conn)
        results = memory_hybrid_search(
            search_conn, tmp_path, "OOM pod", limit=10, connector_name="k8s"
        )
        assert all(r["connector_name"] == "k8s" for r in results)
