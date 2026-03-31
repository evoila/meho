"""KnowledgeStore: dual-write CRUD for SQLite + ChromaDB.

SQLite is the source of truth for knowledge chunks. ChromaDB is a search
cache for semantic similarity. The rebuild() method re-embeds all chunks
from SQLite into ChromaDB without re-parsing original files.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import structlog

from meho_claude.core.database import get_connection
from meho_claude.core.knowledge.chunker import Chunk

logger = structlog.get_logger()


class KnowledgeStore:
    """Dual-write knowledge store backed by SQLite + ChromaDB."""

    def __init__(self, state_dir: Path) -> None:
        """Initialize the store.

        Args:
            state_dir: Path to the meho state directory (~/.meho or test tmpdir).
        """
        self.state_dir = state_dir
        self.conn = get_connection(state_dir / "meho.db")

    def store_chunks(
        self,
        filename: str,
        connector_name: str | None,
        chunks: list[Chunk],
        file_hash: str,
    ) -> int:
        """Store knowledge chunks with dual-write to SQLite + ChromaDB.

        If a source with the same filename + connector_name already exists,
        removes it first (replace behavior for deduplication).

        Args:
            filename: Name of the ingested file.
            connector_name: Connector scope (None for global).
            chunks: List of Chunk objects to store.
            file_hash: SHA-256 hash of the original file.

        Returns:
            Number of chunks stored.
        """
        # Dedup: remove existing source with same filename + connector
        self.remove_source(filename, connector_name)

        # Insert source record
        source_id = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO knowledge_sources (id, filename, connector_name, file_hash, chunk_count) "
            "VALUES (?, ?, ?, ?, ?)",
            (source_id, filename, connector_name, file_hash, len(chunks)),
        )

        # Insert chunk records
        for i, chunk in enumerate(chunks):
            chunk_id = str(uuid.uuid4())
            self.conn.execute(
                "INSERT INTO knowledge_chunks "
                "(id, source_id, chunk_index, content, heading, token_estimate, connector_name) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (chunk_id, source_id, i, chunk.content, chunk.heading, chunk.token_estimate, connector_name),
            )

        self.conn.commit()

        # ChromaDB dual-write (graceful degradation)
        self._embed_chunks(source_id)

        return len(chunks)

    def _embed_chunks(self, source_id: str) -> None:
        """Embed chunks for a source into ChromaDB.

        Reads chunks from SQLite and upserts into the 'knowledge_chunks'
        ChromaDB collection. Never raises -- logs warning on failure.

        Args:
            source_id: The source ID to embed chunks for.
        """
        try:
            from meho_claude.core.search.semantic import get_chroma_client

            client = get_chroma_client(self.state_dir)
            collection = client.get_or_create_collection(
                name="knowledge_chunks",
                metadata={"hnsw:space": "cosine"},
            )

            rows = self.conn.execute(
                "SELECT id, content, heading, connector_name FROM knowledge_chunks "
                "WHERE source_id = ?",
                (source_id,),
            ).fetchall()

            if not rows:
                return

            ids = []
            documents = []
            metadatas = []

            for row in rows:
                ids.append(row["id"])
                # Composite document: heading + content
                doc = f"{row['heading']} {row['content']}".strip()
                documents.append(doc)
                metadatas.append({
                    "connector_name": row["connector_name"] or "__global__",
                    "source_id": source_id,
                })

            collection.upsert(ids=ids, documents=documents, metadatas=metadatas)

        except Exception as exc:
            logger.warning(
                "knowledge_embedding_failed",
                source_id=source_id,
                error=str(exc),
            )

    def remove_source(self, filename: str, connector_name: str | None) -> bool:
        """Remove a knowledge source and all its chunks.

        Deletes from both SQLite (CASCADE deletes chunks, triggers clean FTS5)
        and ChromaDB.

        Args:
            filename: Source filename.
            connector_name: Connector scope (None for global).

        Returns:
            True if the source was found and deleted, False otherwise.
        """
        if connector_name is None:
            row = self.conn.execute(
                "SELECT id FROM knowledge_sources WHERE filename = ? AND connector_name IS NULL",
                (filename,),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT id FROM knowledge_sources WHERE filename = ? AND connector_name = ?",
                (filename, connector_name),
            ).fetchone()

        if not row:
            return False

        source_id = row["id"]

        # Get chunk IDs for ChromaDB cleanup
        chunk_ids = [
            r["id"]
            for r in self.conn.execute(
                "SELECT id FROM knowledge_chunks WHERE source_id = ?",
                (source_id,),
            ).fetchall()
        ]

        # Delete from SQLite (CASCADE handles chunks, triggers handle FTS5)
        self.conn.execute("DELETE FROM knowledge_sources WHERE id = ?", (source_id,))
        self.conn.commit()

        # Try to delete from ChromaDB
        if chunk_ids:
            try:
                from meho_claude.core.search.semantic import get_chroma_client

                client = get_chroma_client(self.state_dir)
                collection = client.get_or_create_collection(
                    name="knowledge_chunks",
                    metadata={"hnsw:space": "cosine"},
                )
                collection.delete(ids=chunk_ids)
            except Exception as exc:
                logger.warning(
                    "knowledge_chromadb_delete_failed",
                    source_id=source_id,
                    error=str(exc),
                )

        return True

    def get_stats(self) -> dict:
        """Get knowledge store statistics.

        Returns:
            Dict with total_sources, total_chunks, and by_connector breakdown.
        """
        total_sources = self.conn.execute(
            "SELECT COUNT(*) FROM knowledge_sources"
        ).fetchone()[0]

        total_chunks = self.conn.execute(
            "SELECT COUNT(*) FROM knowledge_chunks"
        ).fetchone()[0]

        by_connector_rows = self.conn.execute(
            "SELECT "
            "  COALESCE(ks.connector_name, '__global__') AS connector, "
            "  COUNT(DISTINCT ks.id) AS sources, "
            "  SUM(ks.chunk_count) AS chunks "
            "FROM knowledge_sources ks "
            "GROUP BY COALESCE(ks.connector_name, '__global__') "
            "ORDER BY connector"
        ).fetchall()

        by_connector = [
            {
                "connector": row["connector"],
                "sources": row["sources"],
                "chunks": row["chunks"],
            }
            for row in by_connector_rows
        ]

        return {
            "total_sources": total_sources,
            "total_chunks": total_chunks,
            "by_connector": by_connector,
        }

    def rebuild(self) -> int:
        """Re-embed all chunks from SQLite into ChromaDB.

        Deletes and recreates the ChromaDB knowledge_chunks collection,
        then batch upserts all chunks from SQLite.

        Returns:
            Total number of chunks re-embedded.
        """
        from meho_claude.core.search.semantic import get_chroma_client

        client = get_chroma_client(self.state_dir)

        # Delete and recreate collection
        try:
            client.delete_collection("knowledge_chunks")
        except Exception:
            pass

        collection = client.get_or_create_collection(
            name="knowledge_chunks",
            metadata={"hnsw:space": "cosine"},
        )

        # Read all chunks from SQLite
        rows = self.conn.execute(
            "SELECT id, content, heading, connector_name, source_id "
            "FROM knowledge_chunks"
        ).fetchall()

        if not rows:
            return 0

        ids = []
        documents = []
        metadatas = []

        for row in rows:
            ids.append(row["id"])
            doc = f"{row['heading']} {row['content']}".strip()
            documents.append(doc)
            metadatas.append({
                "connector_name": row["connector_name"] or "__global__",
                "source_id": row["source_id"],
            })

        collection.upsert(ids=ids, documents=documents, metadatas=metadatas)

        return len(rows)

    def close(self) -> None:
        """Close the SQLite connection."""
        self.conn.close()
