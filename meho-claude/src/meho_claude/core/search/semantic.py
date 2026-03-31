"""ChromaDB semantic search over operations.

Uses sentence-transformer embeddings (via ChromaDB's default embedding function)
for semantic similarity search. Operations are indexed with composite documents
built from display_name, description, and tags.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import chromadb

if TYPE_CHECKING:
    from chromadb.api.models.Collection import Collection


def get_chroma_client(state_dir: Path) -> chromadb.ClientAPI:
    """Get a persistent ChromaDB client.

    Args:
        state_dir: Path to the meho state directory (~/.meho).

    Returns:
        PersistentClient storing data at state_dir/chroma.
    """
    chroma_path = state_dir / "chroma"
    chroma_path.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(chroma_path))


def get_operations_collection(client: chromadb.ClientAPI) -> Collection:
    """Get or create the 'operations' collection with cosine distance.

    Args:
        client: ChromaDB client.

    Returns:
        Collection configured for cosine similarity search.
    """
    return client.get_or_create_collection(
        name="operations",
        metadata={"hnsw:space": "cosine"},
    )


def index_operations(
    collection: Collection,
    operations: list[dict],
    connector_name: str,
) -> None:
    """Index operations into ChromaDB for semantic search.

    Removes existing operations for the connector before adding (clean re-index).
    Each operation is stored with id="{connector}:{operation_id}" and a composite
    document built from display_name, description, and tags.

    Args:
        collection: ChromaDB collection to index into.
        operations: List of operation dicts with connector_name, operation_id,
            display_name, description, trust_tier, tags, db_id.
        connector_name: Connector name for cleanup and filtering.
    """
    # Delete existing docs for this connector
    try:
        existing = collection.get(where={"connector_name": connector_name})
        if existing["ids"]:
            collection.delete(ids=existing["ids"])
    except Exception:
        # Collection might be empty or have no matching docs
        pass

    if not operations:
        return

    ids = []
    documents = []
    metadatas = []

    for op in operations:
        op_id = f"{op['connector_name']}:{op['operation_id']}"
        tags = op.get("tags", [])
        tags_str = " ".join(tags) if isinstance(tags, list) else str(tags)
        doc = f"{op['display_name']} {op.get('description', '')} {tags_str}".strip()

        ids.append(op_id)
        documents.append(doc)
        metadatas.append({
            "connector_name": op["connector_name"],
            "operation_id": op["operation_id"],
            "trust_tier": op.get("trust_tier", "READ"),
            "db_id": op.get("db_id", 0),
        })

    collection.upsert(ids=ids, documents=documents, metadatas=metadatas)


def search_semantic(
    collection: Collection,
    query: str,
    limit: int = 20,
) -> list[dict]:
    """Search operations using ChromaDB semantic similarity.

    Args:
        collection: ChromaDB operations collection.
        query: Natural language search query.
        limit: Maximum number of results.

    Returns:
        List of dicts with id, connector_name, operation_id, trust_tier, distance.
        Ordered by distance (closest first).
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
            "id": meta.get("db_id", 0),
            "connector_name": meta["connector_name"],
            "operation_id": meta["operation_id"],
            "trust_tier": meta["trust_tier"],
            "distance": distance,
        })

    return output
