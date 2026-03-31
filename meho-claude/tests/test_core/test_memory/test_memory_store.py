"""Tests for MemoryStore dual-write (SQLite + ChromaDB)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from meho_claude.core.memory.store import MemoryStore


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply all meho migrations to an in-memory connection."""
    from importlib import resources

    for name in ("001_initial.sql", "002_operations.sql", "003_knowledge.sql", "004_memory.sql"):
        sql = resources.files("meho_claude.db.migrations.meho").joinpath(name).read_text()
        conn.executescript(sql)


class TestMemoryStore:
    """Tests for MemoryStore class."""

    @pytest.fixture()
    def store(self, tmp_path: Path) -> MemoryStore:
        """Create a MemoryStore with a temporary database."""
        db_path = tmp_path / "meho.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        _apply_migrations(conn)
        conn.close()
        return MemoryStore(tmp_path)

    def test_store_memory_inserts_and_returns_dict(self, store: MemoryStore):
        """store_memory inserts into SQLite and returns memory dict with id."""
        mem = store.store_memory("OOM kills on pod X were caused by memory limit too low")
        assert "id" in mem
        assert mem["content"] == "OOM kills on pod X were caused by memory limit too low"
        assert "created_at" in mem

        # Verify it's actually in the database
        row = store.conn.execute(
            "SELECT * FROM memories WHERE id = ?", (mem["id"],)
        ).fetchone()
        assert row is not None
        assert row["content"] == mem["content"]

    def test_store_memory_global_when_no_connector(self, store: MemoryStore):
        """store_memory with connector_name=None stores as global."""
        mem = store.store_memory("Global memory")
        assert mem["connector_name"] is None

        row = store.conn.execute(
            "SELECT * FROM memories WHERE id = ?", (mem["id"],)
        ).fetchone()
        assert row["connector_name"] is None

    def test_store_memory_with_connector(self, store: MemoryStore):
        """store_memory with connector_name stores with connector scope."""
        mem = store.store_memory("K8s memory", connector_name="k8s-prod")
        assert mem["connector_name"] == "k8s-prod"

    def test_store_memory_with_tags(self, store: MemoryStore):
        """store_memory with tags stores comma-separated tag string."""
        mem = store.store_memory("Some issue", tags="pattern,resolution")
        assert mem["tags"] == "pattern,resolution"

        row = store.conn.execute(
            "SELECT * FROM memories WHERE id = ?", (mem["id"],)
        ).fetchone()
        assert row["tags"] == "pattern,resolution"

    def test_get_memory_returns_memory(self, store: MemoryStore):
        """get_memory returns memory by id."""
        mem = store.store_memory("Find this memory")
        result = store.get_memory(mem["id"])
        assert result is not None
        assert result["content"] == "Find this memory"
        assert result["id"] == mem["id"]

    def test_get_memory_not_found_returns_none(self, store: MemoryStore):
        """get_memory returns None if not found."""
        result = store.get_memory("nonexistent-id")
        assert result is None

    def test_list_memories_returns_all_sorted(self, store: MemoryStore):
        """list_memories returns all memories sorted by created_at desc."""
        store.store_memory("First memory")
        store.store_memory("Second memory")
        store.store_memory("Third memory")

        memories = store.list_memories()
        assert len(memories) == 3
        # Most recent first
        assert memories[0]["content"] == "Third memory"

    def test_list_memories_filters_by_connector(self, store: MemoryStore):
        """list_memories with connector_name filters to that connector only."""
        store.store_memory("K8s memory", connector_name="k8s")
        store.store_memory("VMware memory", connector_name="vmware")
        store.store_memory("Global memory")

        k8s_memories = store.list_memories(connector_name="k8s")
        assert len(k8s_memories) == 1
        assert k8s_memories[0]["connector_name"] == "k8s"

    def test_forget_memory_deletes_and_returns_true(self, store: MemoryStore):
        """forget_memory deletes from SQLite and returns True."""
        mem = store.store_memory("Delete me")
        result = store.forget_memory(mem["id"])
        assert result is True

        # Verify it's gone
        row = store.conn.execute(
            "SELECT * FROM memories WHERE id = ?", (mem["id"],)
        ).fetchone()
        assert row is None

    def test_forget_memory_not_found_returns_false(self, store: MemoryStore):
        """forget_memory returns False if not found."""
        result = store.forget_memory("nonexistent-id")
        assert result is False

    def test_dual_write_chromadb_upsert(self, store: MemoryStore):
        """store_memory also upserts into ChromaDB (test with mocked ChromaDB)."""
        mock_client = MagicMock()
        mock_collection = MagicMock()
        mock_client.get_or_create_collection.return_value = mock_collection

        with patch(
            "meho_claude.core.search.semantic.get_chroma_client",
            return_value=mock_client,
        ):
            mem = store.store_memory("A memory with embeddings", connector_name="k8s", tags="issue")

        mock_collection.upsert.assert_called_once()
        call_kwargs = mock_collection.upsert.call_args
        assert mem["id"] in call_kwargs[1]["ids"] or mem["id"] in call_kwargs[0][0] if call_kwargs[0] else mem["id"] in call_kwargs[1]["ids"]

    def test_forget_memory_cleans_chromadb(self, store: MemoryStore):
        """forget_memory also deletes from ChromaDB."""
        mem = store.store_memory("Delete from both stores")

        mock_client = MagicMock()
        mock_collection = MagicMock()
        mock_client.get_or_create_collection.return_value = mock_collection

        with patch(
            "meho_claude.core.search.semantic.get_chroma_client",
            return_value=mock_client,
        ):
            store.forget_memory(mem["id"])

        mock_collection.delete.assert_called_once()

    def test_rebuild(self, store: MemoryStore):
        """rebuild re-embeds all memories from SQLite to ChromaDB."""
        store.store_memory("Memory one", connector_name="k8s")
        store.store_memory("Memory two", connector_name="vmware")

        mock_client = MagicMock()
        mock_collection = MagicMock()
        mock_client.get_or_create_collection.return_value = mock_collection

        with patch(
            "meho_claude.core.search.semantic.get_chroma_client",
            return_value=mock_client,
        ):
            count = store.rebuild()

        assert count == 2
        mock_client.delete_collection.assert_called_once_with("memories")
        mock_collection.upsert.assert_called_once()

    def test_close(self, store: MemoryStore):
        """close() closes the SQLite connection."""
        store.close()
        with pytest.raises(Exception):
            store.conn.execute("SELECT 1")
