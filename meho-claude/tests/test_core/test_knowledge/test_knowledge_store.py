"""Tests for KnowledgeStore dual-write (SQLite + ChromaDB)."""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from meho_claude.core.knowledge.chunker import Chunk
from meho_claude.core.knowledge.store import KnowledgeStore


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply knowledge migration SQL to an in-memory connection."""
    from importlib import resources

    for name in ("001_initial.sql", "002_operations.sql", "003_knowledge.sql", "004_memory.sql"):
        sql = resources.files("meho_claude.db.migrations.meho").joinpath(name).read_text()
        conn.executescript(sql)


def _make_chunks(count: int = 3) -> list[Chunk]:
    """Create sample Chunk objects for testing."""
    return [
        Chunk(
            content=f"Content for chunk {i}",
            heading=f"# Heading {i}",
            chunk_index=i,
            token_estimate=10,
        )
        for i in range(count)
    ]


class TestKnowledgeStore:
    """Tests for KnowledgeStore class."""

    @pytest.fixture()
    def store(self, tmp_path: Path) -> KnowledgeStore:
        """Create a KnowledgeStore with a temporary database."""
        db_path = tmp_path / "meho.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        _apply_migrations(conn)
        conn.close()
        return KnowledgeStore(tmp_path)

    def test_store_chunks_inserts_source_and_chunks(self, store: KnowledgeStore):
        """store_chunks inserts source + chunks into SQLite and returns chunk count."""
        chunks = _make_chunks(3)
        count = store.store_chunks("doc.md", "my-connector", chunks, "abc123hash")
        assert count == 3

        # Verify source record exists
        row = store.conn.execute(
            "SELECT * FROM knowledge_sources WHERE filename = ?", ("doc.md",)
        ).fetchone()
        assert row is not None
        assert row["connector_name"] == "my-connector"
        assert row["file_hash"] == "abc123hash"
        assert row["chunk_count"] == 3

        # Verify chunk records exist
        chunk_rows = store.conn.execute(
            "SELECT * FROM knowledge_chunks ORDER BY chunk_index"
        ).fetchall()
        assert len(chunk_rows) == 3

    def test_store_chunks_dedup_replaces_old_chunks(self, store: KnowledgeStore):
        """store_chunks with same filename+connector replaces old chunks (dedup)."""
        chunks1 = _make_chunks(2)
        store.store_chunks("doc.md", "my-conn", chunks1, "hash1")

        chunks2 = _make_chunks(5)
        store.store_chunks("doc.md", "my-conn", chunks2, "hash2")

        # Should have only 5 chunks now (not 7)
        total = store.conn.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0]
        assert total == 5

        # Source should be updated
        sources = store.conn.execute("SELECT * FROM knowledge_sources").fetchall()
        assert len(sources) == 1
        assert sources[0]["file_hash"] == "hash2"

    def test_store_chunks_global_connector(self, store: KnowledgeStore):
        """store_chunks with connector_name=None stores as global."""
        chunks = _make_chunks(2)
        count = store.store_chunks("doc.md", None, chunks, "hashglobal")
        assert count == 2

        row = store.conn.execute("SELECT * FROM knowledge_sources").fetchone()
        assert row["connector_name"] is None

    def test_remove_source_deletes_chunks(self, store: KnowledgeStore):
        """remove_source deletes all chunks for a source from SQLite."""
        chunks = _make_chunks(3)
        store.store_chunks("doc.md", "conn", chunks, "hash1")

        result = store.remove_source("doc.md", "conn")
        assert result is True

        # Verify source and chunks are gone
        sources = store.conn.execute("SELECT COUNT(*) FROM knowledge_sources").fetchone()[0]
        assert sources == 0
        chunk_count = store.conn.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0]
        assert chunk_count == 0

    def test_remove_source_nonexistent_returns_false(self, store: KnowledgeStore):
        """remove_source for nonexistent source returns False."""
        result = store.remove_source("nonexistent.md", None)
        assert result is False

    def test_get_stats(self, store: KnowledgeStore):
        """get_stats returns dict with total_sources, total_chunks, by_connector breakdown."""
        store.store_chunks("doc1.md", "conn-a", _make_chunks(3), "h1")
        store.store_chunks("doc2.md", "conn-a", _make_chunks(2), "h2")
        store.store_chunks("doc3.md", "conn-b", _make_chunks(4), "h3")

        stats = store.get_stats()
        assert stats["total_sources"] == 3
        assert stats["total_chunks"] == 9
        assert len(stats["by_connector"]) == 2

        # Find conn-a stats
        conn_a = next(c for c in stats["by_connector"] if c["connector"] == "conn-a")
        assert conn_a["sources"] == 2
        assert conn_a["chunks"] == 5

    def test_rebuild(self, store: KnowledgeStore):
        """rebuild re-embeds all chunks from SQLite (test with mocked ChromaDB)."""
        store.store_chunks("doc.md", "conn", _make_chunks(3), "hash")

        # Mock ChromaDB for rebuild
        mock_client = MagicMock()
        mock_collection = MagicMock()
        mock_client.get_or_create_collection.return_value = mock_collection

        with patch(
            "meho_claude.core.search.semantic.get_chroma_client", return_value=mock_client
        ):
            count = store.rebuild()

        assert count == 3
        # Verify ChromaDB was called
        mock_client.delete_collection.assert_called_once_with("knowledge_chunks")
        mock_collection.upsert.assert_called_once()

    def test_close(self, store: KnowledgeStore):
        """close() closes the SQLite connection."""
        store.close()
        with pytest.raises(Exception):
            store.conn.execute("SELECT 1")
