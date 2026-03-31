"""MemoryStore: dual-write CRUD for SQLite + ChromaDB.

SQLite is the source of truth for memories. ChromaDB is a search cache
for semantic similarity. The rebuild() method re-embeds all memories
from SQLite into ChromaDB without data loss.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import structlog

from meho_claude.core.database import get_connection

logger = structlog.get_logger()


class MemoryStore:
    """Dual-write memory store backed by SQLite + ChromaDB."""

    def __init__(self, state_dir: Path) -> None:
        """Initialize the store.

        Args:
            state_dir: Path to the meho state directory (~/.meho or test tmpdir).
        """
        self.state_dir = state_dir
        self.conn = get_connection(state_dir / "meho.db")

    def store_memory(
        self,
        content: str,
        connector_name: str | None = None,
        tags: str = "",
    ) -> dict:
        """Store a memory with dual-write to SQLite + ChromaDB.

        Args:
            content: Memory text content.
            connector_name: Optional connector scope (None for global).
            tags: Comma-separated tags string.

        Returns:
            Dict with id, content, connector_name, tags, created_at.
        """
        memory_id = str(uuid4())

        self.conn.execute(
            "INSERT INTO memories (id, content, connector_name, tags) "
            "VALUES (?, ?, ?, ?)",
            (memory_id, content, connector_name, tags),
        )
        self.conn.commit()

        # Read back to get server-generated created_at
        row = self.conn.execute(
            "SELECT * FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()

        result = {
            "id": row["id"],
            "content": row["content"],
            "connector_name": row["connector_name"],
            "tags": row["tags"],
            "created_at": row["created_at"],
        }

        # ChromaDB dual-write (graceful degradation)
        self._embed_memory(memory_id, content, connector_name, tags)

        return result

    def _embed_memory(
        self,
        memory_id: str,
        content: str,
        connector_name: str | None,
        tags: str,
    ) -> None:
        """Embed a single memory into ChromaDB.

        Never raises -- logs warning on failure.
        """
        try:
            from meho_claude.core.search.semantic import get_chroma_client

            client = get_chroma_client(self.state_dir)
            collection = client.get_or_create_collection(
                name="memories",
                metadata={"hnsw:space": "cosine"},
            )
            collection.upsert(
                ids=[memory_id],
                documents=[content],
                metadatas=[{
                    "connector_name": connector_name or "__global__",
                    "tags": tags,
                }],
            )
        except Exception as exc:
            logger.warning(
                "memory_embedding_failed",
                memory_id=memory_id,
                error=str(exc),
            )

    def get_memory(self, memory_id: str) -> dict | None:
        """Get a memory by ID.

        Args:
            memory_id: UUID of the memory.

        Returns:
            Dict with memory fields, or None if not found.
        """
        row = self.conn.execute(
            "SELECT * FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()

        if row is None:
            return None

        return {
            "id": row["id"],
            "content": row["content"],
            "connector_name": row["connector_name"],
            "tags": row["tags"],
            "created_at": row["created_at"],
        }

    def list_memories(self, connector_name: str | None = None) -> list[dict]:
        """List all memories, optionally filtered by connector.

        Args:
            connector_name: Optional connector filter.

        Returns:
            List of memory dicts, sorted by created_at desc.
        """
        if connector_name is not None:
            rows = self.conn.execute(
                "SELECT * FROM memories WHERE connector_name = ? "
                "ORDER BY created_at DESC, rowid DESC",
                (connector_name,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM memories ORDER BY created_at DESC, rowid DESC"
            ).fetchall()

        return [
            {
                "id": row["id"],
                "content": row["content"],
                "connector_name": row["connector_name"],
                "tags": row["tags"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def forget_memory(self, memory_id: str) -> bool:
        """Delete a memory from both SQLite and ChromaDB.

        Args:
            memory_id: UUID of the memory to forget.

        Returns:
            True if memory was found and deleted, False otherwise.
        """
        # Check if memory exists first
        row = self.conn.execute(
            "SELECT id FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()

        if row is None:
            return False

        # Delete from SQLite (triggers handle FTS5 cleanup)
        self.conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        self.conn.commit()

        # Try to delete from ChromaDB
        try:
            from meho_claude.core.search.semantic import get_chroma_client

            client = get_chroma_client(self.state_dir)
            collection = client.get_or_create_collection(
                name="memories",
                metadata={"hnsw:space": "cosine"},
            )
            collection.delete(ids=[memory_id])
        except Exception as exc:
            logger.warning(
                "memory_chromadb_delete_failed",
                memory_id=memory_id,
                error=str(exc),
            )

        return True

    def rebuild(self) -> int:
        """Re-embed all memories from SQLite into ChromaDB.

        Deletes and recreates the 'memories' ChromaDB collection,
        then batch upserts all memories from SQLite.

        Returns:
            Total number of memories re-embedded.
        """
        from meho_claude.core.search.semantic import get_chroma_client

        client = get_chroma_client(self.state_dir)

        # Delete and recreate collection
        try:
            client.delete_collection("memories")
        except Exception:
            pass

        collection = client.get_or_create_collection(
            name="memories",
            metadata={"hnsw:space": "cosine"},
        )

        # Read all memories from SQLite
        rows = self.conn.execute(
            "SELECT id, content, connector_name, tags FROM memories"
        ).fetchall()

        if not rows:
            return 0

        ids = []
        documents = []
        metadatas = []

        for row in rows:
            ids.append(row["id"])
            documents.append(row["content"])
            metadatas.append({
                "connector_name": row["connector_name"] or "__global__",
                "tags": row["tags"],
            })

        collection.upsert(ids=ids, documents=documents, metadatas=metadatas)

        return len(rows)

    def close(self) -> None:
        """Close the SQLite connection."""
        self.conn.close()
