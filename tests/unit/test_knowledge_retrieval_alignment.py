# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for retrieval-text alignment between indexing and search.

The cross-encoder reranker is intentionally absent in the fastembed
preview path. These tests cover that the retrieval text MEHO indexes is
the same shape MEHO surfaces back to callers, and that connector-scoped
retrieval correctly filters at the repository layer.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from meho_app.core.auth_context import UserContext
from meho_app.modules.knowledge.hybrid_search import PostgresFTSHybridService
from meho_app.modules.knowledge.knowledge_store import KnowledgeStore
from meho_app.modules.knowledge.retrieval_context import build_retrieval_text
from meho_app.modules.knowledge.schemas import ChunkMetadata, KnowledgeChunk, KnowledgeChunkCreate


def _make_chunk(
    *,
    text: str,
    source_uri: str,
    connector_id: str | None = None,
    connector_type_scope: str | None = None,
    scope_type: str = "instance",
    metadata: ChunkMetadata | None = None,
) -> KnowledgeChunk:
    now = datetime.now(tz=UTC)
    return KnowledgeChunk(
        id=str(uuid4()),
        text=text,
        tenant_id="tenant-1",
        connector_id=connector_id,
        user_id=None,
        roles=[],
        groups=[],
        tags=[],
        source_uri=source_uri,
        scope_type=scope_type,
        connector_type_scope=connector_type_scope,
        search_metadata=metadata,
        knowledge_type="documentation",
        priority=0,
        expires_at=None,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_add_chunk_embeds_retrieval_text() -> None:
    """Indexing should embed the Farseer-style retrieval text."""
    metadata = ChunkMetadata(
        document_name="soccer.pdf",
        heading_hierarchy=["11 Conclusion"],
        page_number=11,
        page_numbers=[11],
        page_start=11,
        page_end=11,
    )
    chunk_create = KnowledgeChunkCreate(
        text="This methodology provides a flexible, mathematically grounded infrastructure.",
        tenant_id="tenant-1",
        source_uri="s3://bucket/documents/soccer.pdf",
        search_metadata=metadata,
    )
    created_chunk = _make_chunk(
        text=chunk_create.text,
        source_uri=chunk_create.source_uri or "",
        metadata=metadata,
    )

    repository = Mock()
    repository.create_chunk = AsyncMock(return_value=created_chunk)
    embedding_provider = Mock()
    embedding_provider.embed_text = AsyncMock(return_value=[0.1, 0.2, 0.3])

    store = KnowledgeStore(repository=repository, embedding_provider=embedding_provider)
    await store.add_chunk(chunk_create)

    expected_text = build_retrieval_text(
        chunk_create.text,
        filename="soccer.pdf",
        heading_path=["11 Conclusion"],
        page_number=11,
        page_start=11,
        page_end=11,
    )
    embedding_provider.embed_text.assert_awaited_once_with(expected_text)


@pytest.mark.asyncio
async def test_search_by_connector_passes_connector_filter() -> None:
    """Connector-scoped retrieval should filter at the repository layer.

    With the reranker absent in the preview, ``score_threshold`` is passed
    through unchanged (no widening to 0.0 to feed a cross-encoder).
    """
    connector_id = str(uuid4())
    chunk = _make_chunk(
        text="Connector-specific chunk",
        source_uri="s3://bucket/documents/scoped.pdf",
        connector_id=connector_id,
        metadata=ChunkMetadata(document_name="scoped.pdf"),
    )

    repository = Mock()
    repository.search_by_embedding = AsyncMock(return_value=[(chunk, 0.55)])
    embedding_provider = Mock()
    embedding_provider.embed_text = AsyncMock(return_value=[0.1, 0.2, 0.3])
    hybrid_search_service = Mock()

    store = KnowledgeStore(
        repository=repository,
        embedding_provider=embedding_provider,
        hybrid_search_service=hybrid_search_service,
    )
    user_context = UserContext(
        tenant_id="tenant-1",
        user_id="user-1",
        roles=["admin"],
        groups=[],
    )

    results = await store.search_by_connector(
        query="specific query",
        user_context=user_context,
        connector_id=connector_id,
        top_k=3,
        score_threshold=0.6,
    )

    assert [c.id for c in results] == [chunk.id]
    repository.search_by_embedding.assert_awaited_once()
    assert repository.search_by_embedding.await_args.kwargs["connector_id"] == connector_id
    # No reranker -> threshold passes through unchanged.
    assert repository.search_by_embedding.await_args.kwargs["score_threshold"] == 0.6


@pytest.mark.asyncio
async def test_search_ranked_by_connector_returns_metadata_and_score() -> None:
    """Connector-scoped ranked results expose retrieval metadata for UI use."""
    connector_id = str(uuid4())
    chunk = _make_chunk(
        text="Restart the connector if sync stalls.",
        source_uri="s3://bucket/documents/scoped.pdf#chunk=3",
        connector_id=connector_id,
        metadata=ChunkMetadata(
            document_name="scoped.pdf",
            heading_hierarchy=["Runbook", "Recovery"],
            page_number=5,
            page_numbers=[5],
            page_start=5,
            page_end=5,
        ),
    )

    repository = Mock()
    repository.search_by_embedding = AsyncMock(return_value=[(chunk, 0.88)])
    embedding_provider = Mock()
    embedding_provider.embed_text = AsyncMock(return_value=[0.1, 0.2, 0.3])

    store = KnowledgeStore(
        repository=repository,
        embedding_provider=embedding_provider,
    )
    user_context = UserContext(
        tenant_id="tenant-1",
        user_id="user-1",
        roles=["admin"],
        groups=[],
    )

    results = await store.search_ranked_by_connector(
        query="how do I recover sync",
        user_context=user_context,
        connector_id=connector_id,
        top_k=3,
        score_threshold=0.6,
    )

    assert len(results) == 1
    assert results[0]["score"] == 0.88
    assert results[0]["filename"] == "scoped.pdf"
    assert results[0]["heading_path"] == ["Runbook", "Recovery"]
    assert results[0]["section_header"] == "Recovery"
    assert results[0]["page_number"] == 5
    assert results[0]["source_chunk_index"] == 3


@pytest.mark.asyncio
async def test_hybrid_adaptive_search_returns_rrf_fused_results() -> None:
    """``adaptive_search`` runs BM25 + vector RRF (no cross-encoder rerank)."""
    chunk_a = _make_chunk(
        text="Match Result (3-way), Correct Score",
        source_uri="s3://bucket/documents/soccer.pdf",
        metadata=ChunkMetadata(
            document_name="soccer.pdf",
            heading_hierarchy=["9 Odds Construction"],
            page_number=9,
            page_numbers=[9],
            page_start=9,
            page_end=9,
        ),
    )
    chunk_b = _make_chunk(
        text="Hence the 3-way market probabilities.",
        source_uri="s3://bucket/documents/soccer.pdf",
        metadata=ChunkMetadata(
            document_name="soccer.pdf",
            heading_hierarchy=["9 Odds Construction"],
            page_number=8,
            page_numbers=[8],
            page_start=8,
            page_end=8,
        ),
    )

    repository = Mock()
    repository.search_by_embedding = AsyncMock(return_value=[(chunk_a, 0.41), (chunk_b, 0.39)])
    repository.session = Mock()

    embedding_provider = Mock()
    embedding_provider.embed_text = AsyncMock(return_value=[0.2, 0.4, 0.6])

    bm25_service = Mock()
    bm25_service.search = AsyncMock(return_value=[])

    service = PostgresFTSHybridService(
        repository=repository,
        embeddings=embedding_provider,
        bm25_service=bm25_service,
    )
    user_context = UserContext(
        tenant_id="tenant-1",
        user_id="user-1",
        roles=["admin"],
        groups=[],
    )

    results = await service.adaptive_search(
        query="who is the favorite",
        user_context=user_context,
        top_k=2,
        score_threshold=0.6,
    )

    # Reranker is absent -> RRF order matches the underlying semantic order.
    assert [r["id"] for r in results] == [str(chunk_a.id), str(chunk_b.id)]
    embedding_provider.embed_text.assert_awaited_once_with("who is the favorite")
    repository.search_by_embedding.assert_awaited_once()
    bm25_service.search.assert_awaited_once()
    assert service.reranker is None
