# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Arrow IPC codec for serializing and deserializing knowledge chunks.

Chunks are serialized as Arrow IPC files with zstd compression for
transfer between the MEHO API and ephemeral ingestion workers via
object storage (S3/MinIO presigned URLs).

The schema includes chunk text, 1024-dimensional float32 embeddings,
and all ChunkMetadata fields needed to reconstruct KnowledgeChunkCreate
objects on the receiving side.
"""

from typing import Any

import pyarrow as pa
import pyarrow.ipc as ipc

# Schema version for forward compatibility.
# Increment when adding/removing/renaming columns.
SCHEMA_VERSION: int = 1

# Embedding dimension matches Voyage AI voyage-4-large (1024D).
_EMBEDDING_DIM: int = 1024

# Arrow schema for the chunk transfer format.
# Columns map to ChunkMetadata fields plus chunk_text and embedding.
ARROW_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("chunk_text", pa.utf8()),
        pa.field("embedding", pa.list_(pa.float32(), _EMBEDDING_DIM)),
        pa.field("heading_stack", pa.list_(pa.utf8())),
        pa.field("page_numbers", pa.list_(pa.int32())),
        pa.field("document_name", pa.utf8()),
        pa.field("chapter", pa.utf8()),
        pa.field("section", pa.utf8()),
        pa.field("subsection", pa.utf8()),
        pa.field("content_type", pa.utf8()),
        pa.field("has_table", pa.bool_()),
        pa.field("has_code_example", pa.bool_()),
        pa.field("has_json_example", pa.bool_()),
        pa.field("keywords", pa.list_(pa.utf8())),
        pa.field("resource_type", pa.utf8()),
    ]
).with_metadata({"schema_version": str(SCHEMA_VERSION)})


def serialize_chunks(
    chunks: list[tuple[str, dict[str, Any]]],
    embeddings: list[list[float]],
) -> bytes:
    """Serialize knowledge chunks and embeddings to Arrow IPC bytes with zstd compression.

    Args:
        chunks: List of (text, metadata_dict) tuples. Metadata keys map to
            ChunkMetadata fields; missing keys default to None/False/[].
        embeddings: List of 1024-dimensional float32 embedding vectors,
            one per chunk.

    Returns:
        Arrow IPC file bytes compressed with zstd.

    Raises:
        ValueError: If len(chunks) != len(embeddings) or any embedding
            has wrong dimension.
    """
    if len(chunks) != len(embeddings):
        msg = f"chunks ({len(chunks)}) and embeddings ({len(embeddings)}) must have same length"
        raise ValueError(msg)

    n = len(chunks)

    # Validate embedding dimensions
    for i, emb in enumerate(embeddings):
        if len(emb) != _EMBEDDING_DIM:
            msg = f"Embedding at index {i} has {len(emb)} dimensions, expected {_EMBEDDING_DIM}"
            raise ValueError(msg)

    # Extract arrays from chunks
    texts: list[str] = []
    heading_stacks: list[list[str]] = []
    page_numbers_list: list[list[int]] = []
    document_names: list[str | None] = []
    chapters: list[str | None] = []
    sections: list[str | None] = []
    subsections: list[str | None] = []
    content_types: list[str | None] = []
    has_tables: list[bool] = []
    has_code_examples: list[bool] = []
    has_json_examples: list[bool] = []
    keywords_list: list[list[str]] = []
    resource_types: list[str | None] = []

    for text, meta in chunks:
        texts.append(text)
        heading_stacks.append(meta.get("heading_stack", []))
        page_numbers_list.append(meta.get("page_numbers", []))
        document_names.append(meta.get("document_name"))
        chapters.append(meta.get("chapter"))
        sections.append(meta.get("section"))
        subsections.append(meta.get("subsection"))
        content_types.append(meta.get("content_type"))
        has_tables.append(meta.get("has_table", False))
        has_code_examples.append(meta.get("has_code_example", False))
        has_json_examples.append(meta.get("has_json_example", False))
        keywords_list.append(meta.get("keywords", []))
        resource_types.append(meta.get("resource_type"))

    # Build Arrow arrays
    arrays: dict[str, pa.Array] = {
        "chunk_text": pa.array(texts, type=pa.utf8()),
        "embedding": pa.array(embeddings, type=pa.list_(pa.float32(), _EMBEDDING_DIM)),
        "heading_stack": pa.array(heading_stacks, type=pa.list_(pa.utf8())),
        "page_numbers": pa.array(page_numbers_list, type=pa.list_(pa.int32())),
        "document_name": pa.array(document_names, type=pa.utf8()),
        "chapter": pa.array(chapters, type=pa.utf8()),
        "section": pa.array(sections, type=pa.utf8()),
        "subsection": pa.array(subsections, type=pa.utf8()),
        "content_type": pa.array(content_types, type=pa.utf8()),
        "has_table": pa.array(has_tables, type=pa.bool_()),
        "has_code_example": pa.array(has_code_examples, type=pa.bool_()),
        "has_json_example": pa.array(has_json_examples, type=pa.bool_()),
        "keywords": pa.array(keywords_list, type=pa.list_(pa.utf8())),
        "resource_type": pa.array(resource_types, type=pa.utf8()),
    }

    batch = pa.RecordBatch.from_pydict(arrays, schema=ARROW_SCHEMA)

    # Write as IPC file with zstd compression
    sink = pa.BufferOutputStream()
    options = ipc.IpcWriteOptions(compression="zstd")
    writer = ipc.new_file(sink, ARROW_SCHEMA, options=options)
    if n > 0:
        writer.write_batch(batch)
    writer.close()

    return sink.getvalue().to_pybytes()  # type: ignore[no-any-return]


def deserialize_chunks(data: bytes) -> pa.Table:
    """Deserialize Arrow IPC bytes back to a PyArrow Table.

    Validates that the schema version in the file metadata matches
    the current SCHEMA_VERSION.

    Args:
        data: Arrow IPC file bytes (as produced by serialize_chunks).

    Returns:
        PyArrow Table with all chunk columns.

    Raises:
        ValueError: If schema_version in file metadata does not match.
    """
    reader = ipc.open_file(data)
    schema = reader.schema

    # Validate schema version from metadata
    metadata = schema.metadata
    if metadata and b"schema_version" in metadata:
        file_version = int(metadata[b"schema_version"])
        if file_version != SCHEMA_VERSION:
            msg = f"Schema version mismatch: file has v{file_version}, expected v{SCHEMA_VERSION}"
            raise ValueError(msg)

    return reader.read_all()
