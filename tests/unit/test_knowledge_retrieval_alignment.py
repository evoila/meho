# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for Farseer-aligned retrieval text, ranking, and reranking."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from meho_app.core.auth_context import UserContext
from meho_app.modules.knowledge.hybrid_search import PostgresFTSHybridService
from meho_app.modules.knowledge.knowledge_store import KnowledgeStore
from meho_app.modules.knowledge.retrieval_context import build_retrieval_text
from meho_app.modules.knowledge.schemas import ChunkMetadata, KnowledgeChunk, KnowledgeChunkCreate


class _ExecuteResult:
    """Minimal SQLAlchemy result stub for connector lookups."""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


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
async def test_add_chunk_embeds_retrieval_text_with_document_input_type() -> None:
    """Indexing should embed Farseer-style retrieval text as a document."""
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
    embedding_provider.embed_text.assert_awaited_once_with(
        expected_text,
        input_type="document",
    )


@pytest.mark.asyncio
async def test_search_cross_connector_uses_reranker_order_and_retrieval_text() -> None:
    """Cross-connector search should rerank the same retrieval text MEHO indexed."""
    connector_a = str(uuid4())
    connector_b = str(uuid4())
    chunk_a = _make_chunk(
        text="Match Result (3-way), Correct Score",
        source_uri="s3://bucket/documents/soccer.pdf",
        connector_id=connector_a,
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
        text="Hence the 3-way market probabilities. Fair odds are 1 / P(H), 1 / P(D), 1 / P(A).",
        source_uri="s3://bucket/documents/soccer.pdf",
        connector_id=connector_b,
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
    repository.session.execute = AsyncMock(
        return_value=_ExecuteResult(
            [
                (connector_a, "Connector A", "vmware"),
                (connector_b, "Connector B", "vmware"),
            ]
        )
    )

    embedding_provider = Mock()
    embedding_provider.embed_text = AsyncMock(return_value=[0.2, 0.4, 0.6])
    reranker = Mock()
    reranker.rerank = AsyncMock(
        return_value=[
            {"index": 1, "relevance_score": 0.91},
            {"index": 0, "relevance_score": 0.74},
        ]
    )
    hybrid_search_service = Mock()
    hybrid_search_service.reranker = reranker

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

    results = await store.search_cross_connector(
        query="who is the favorite",
        user_context=user_context,
        top_k=2,
    )

    assert [result["id"] for result in results] == [chunk_b.id, chunk_a.id]
    assert [result["score"] for result in results] == [0.91, 0.74]
    assert results[0]["filename"] == "soccer.pdf"
    assert results[0]["heading_path"] == ["9 Odds Construction"]
    assert results[0]["page_start"] == 8
    assert results[0]["page_end"] == 8
    assert results[0]["source_chunk_index"] is None
    repository.search_by_embedding.assert_awaited_once()
    assert repository.search_by_embedding.await_args.kwargs["score_threshold"] == 0.0

    rerank_call = reranker.rerank.await_args
    assert rerank_call.kwargs["documents"] == [
        build_retrieval_text(
            chunk_a.text,
            filename="soccer.pdf",
            heading_path=["9 Odds Construction"],
            page_number=9,
            page_start=9,
            page_end=9,
        ),
        build_retrieval_text(
            chunk_b.text,
            filename="soccer.pdf",
            heading_path=["9 Odds Construction"],
            page_number=8,
            page_start=8,
            page_end=8,
        ),
    ]


@pytest.mark.asyncio
async def test_search_by_connector_passes_connector_filter_to_ranker() -> None:
    """Connector-scoped retrieval should filter before reranking."""
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
    hybrid_search_service.reranker = Mock()

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

    assert [chunk.id for chunk in results] == [chunk.id]
    repository.search_by_embedding.assert_awaited_once()
    assert repository.search_by_embedding.await_args.kwargs["connector_id"] == connector_id
    assert repository.search_by_embedding.await_args.kwargs["score_threshold"] == 0.0


@pytest.mark.asyncio
async def test_search_ranked_by_connector_returns_metadata_and_score() -> None:
    """Connector-scoped ranked results should expose retrieval metadata for UI use."""
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
async def test_hybrid_service_adaptive_search_uses_semantic_rank_and_rerank() -> None:
    """Legacy hybrid entrypoint should delegate to the Farseer-style flow."""
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
        text="Hence the 3-way market probabilities. Fair odds are 1 / P(H), 1 / P(D), 1 / P(A).",
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
    embedding_provider = Mock()
    embedding_provider.embed_text = AsyncMock(return_value=[0.2, 0.4, 0.6])
    reranker = Mock()
    reranker.rerank = AsyncMock(
        return_value=[
            {"index": 1, "relevance_score": 0.93},
            {"index": 0, "relevance_score": 0.71},
        ]
    )

    service = PostgresFTSHybridService(
        repository=repository,
        embeddings=embedding_provider,
        reranker=reranker,
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

    assert [result["id"] for result in results] == [chunk_b.id, chunk_a.id]
    assert [result["score"] for result in results] == [0.93, 0.71]
    embedding_provider.embed_text.assert_awaited_once_with(
        "who is the favorite",
        input_type="query",
    )
    repository.search_by_embedding.assert_awaited_once()
    assert repository.search_by_embedding.await_args.kwargs["score_threshold"] == 0.0
    rerank_call = reranker.rerank.await_args
    assert rerank_call.kwargs["documents"] == [
        build_retrieval_text(
            chunk_a.text,
            filename="soccer.pdf",
            heading_path=["9 Odds Construction"],
            page_number=9,
            page_start=9,
            page_end=9,
        ),
        build_retrieval_text(
            chunk_b.text,
            filename="soccer.pdf",
            heading_path=["9 Odds Construction"],
            page_number=8,
            page_start=8,
            page_end=8,
        ),
    ]
