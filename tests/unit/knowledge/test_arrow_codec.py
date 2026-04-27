# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for Arrow IPC codec (serialize/deserialize knowledge chunks)."""

import pyarrow as pa
import pyarrow.ipc as ipc
import pytest

from meho_app.worker.arrow_codec import (
    ARROW_SCHEMA,
    SCHEMA_VERSION,
    deserialize_chunks,
    serialize_chunks,
)


class TestSerializeDeserializeRoundTrip:
    """Arrow IPC round-trip serialization tests."""

    def test_single_chunk_round_trip(self) -> None:
        """serialize_chunks + deserialize_chunks preserves text, embedding, and metadata."""
        chunks = [("hello world", {"chapter": "Ch1"})]
        embeddings = [[0.1] * 1024]

        data = serialize_chunks(chunks, embeddings)
        table = deserialize_chunks(data)

        assert table.num_rows == 1
        assert table.column("chunk_text")[0].as_py() == "hello world"
        assert len(table.column("embedding")[0].as_py()) == 1024
        assert table.column("chapter")[0].as_py() == "Ch1"

    def test_empty_chunks_round_trip(self) -> None:
        """Empty chunks list serializes/deserializes to 0-row table."""
        data = serialize_chunks([], [])
        table = deserialize_chunks(data)

        assert table.num_rows == 0

    def test_schema_version_in_metadata(self) -> None:
        """Schema version metadata is present in serialized bytes."""
        data = serialize_chunks([], [])
        reader = ipc.open_file(data)
        schema = reader.schema
        metadata = schema.metadata
        assert metadata is not None
        assert b"schema_version" in metadata
        assert metadata[b"schema_version"] == str(SCHEMA_VERSION).encode()

    def test_embedding_dimension_mismatch_raises(self) -> None:
        """Embedding dimension mismatch (e.g., 512D) raises ValueError."""
        chunks = [("text", {})]
        embeddings = [[0.1] * 512]  # Wrong dimension

        with pytest.raises(ValueError, match="1024"):
            serialize_chunks(chunks, embeddings)

    def test_multiple_chunks_round_trip(self) -> None:
        """100 chunks round-trip with correct row count and all metadata fields preserved."""
        chunks = [
            (
                f"chunk {i}",
                {
                    "chapter": f"Chapter {i}",
                    "section": f"Section {i}",
                    "subsection": f"Sub {i}",
                    "content_type": "description",
                    "has_table": i % 2 == 0,
                    "has_code_example": False,
                    "has_json_example": i % 3 == 0,
                    "keywords": [f"kw{i}"],
                    "resource_type": f"type_{i}",
                    "heading_stack": [f"H1_{i}", f"H2_{i}"],
                    "page_numbers": [i, i + 1],
                    "document_name": f"doc_{i}.pdf",
                },
            )
            for i in range(100)
        ]
        embeddings = [[float(i) * 0.001] * 1024 for i in range(100)]

        data = serialize_chunks(chunks, embeddings)
        table = deserialize_chunks(data)

        assert table.num_rows == 100

        # Verify first and last chunk
        assert table.column("chunk_text")[0].as_py() == "chunk 0"
        assert table.column("chunk_text")[99].as_py() == "chunk 99"
        assert table.column("chapter")[0].as_py() == "Chapter 0"
        assert table.column("section")[50].as_py() == "Section 50"
        assert table.column("has_table")[0].as_py() is True
        assert table.column("has_table")[1].as_py() is False
        assert table.column("keywords")[0].as_py() == ["kw0"]
        assert table.column("heading_stack")[0].as_py() == ["H1_0", "H2_0"]
        assert table.column("page_numbers")[0].as_py() == [0, 1]
        assert table.column("document_name")[0].as_py() == "doc_0.pdf"

    def test_zstd_compression_reduces_size(self) -> None:
        """zstd compression actually reduces size vs uncompressed."""
        chunks = [(f"chunk {i} with some text content", {}) for i in range(50)]
        embeddings = [[float(i) * 0.001] * 1024 for i in range(50)]

        compressed = serialize_chunks(chunks, embeddings)

        # Build uncompressed for comparison
        arrays = {
            "chunk_text": pa.array([c[0] for c in chunks], type=pa.utf8()),
            "embedding": pa.array(embeddings, type=pa.list_(pa.float32(), 1024)),
            "heading_stack": pa.array([[] for _ in chunks], type=pa.list_(pa.utf8())),
            "page_numbers": pa.array([[] for _ in chunks], type=pa.list_(pa.int32())),
            "document_name": pa.array([None for _ in chunks], type=pa.utf8()),
            "chapter": pa.array([None for _ in chunks], type=pa.utf8()),
            "section": pa.array([None for _ in chunks], type=pa.utf8()),
            "subsection": pa.array([None for _ in chunks], type=pa.utf8()),
            "content_type": pa.array([None for _ in chunks], type=pa.utf8()),
            "has_table": pa.array([False for _ in chunks], type=pa.bool_()),
            "has_code_example": pa.array([False for _ in chunks], type=pa.bool_()),
            "has_json_example": pa.array([False for _ in chunks], type=pa.bool_()),
            "keywords": pa.array([[] for _ in chunks], type=pa.list_(pa.utf8())),
            "resource_type": pa.array([None for _ in chunks], type=pa.utf8()),
        }
        batch = pa.RecordBatch.from_pydict(arrays, schema=ARROW_SCHEMA)
        sink = pa.BufferOutputStream()
        writer = ipc.new_file(sink, ARROW_SCHEMA)
        writer.write_batch(batch)
        writer.close()
        uncompressed = sink.getvalue().to_pybytes()

        # Compressed should be meaningfully smaller
        assert len(compressed) < len(uncompressed)


class TestSchemaVersion:
    """Schema version constant tests."""

    def test_schema_version_is_integer(self) -> None:
        """SCHEMA_VERSION is an integer."""
        assert isinstance(SCHEMA_VERSION, int)
        assert SCHEMA_VERSION >= 1

    def test_arrow_schema_has_embedding_column(self) -> None:
        """ARROW_SCHEMA has a fixed-size 1024D float32 embedding column."""
        field = ARROW_SCHEMA.field("embedding")
        assert field.type == pa.list_(pa.float32(), 1024)
