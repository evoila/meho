# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for the BM25 + vector RRF fusion in hybrid_search."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from meho_app.modules.knowledge.hybrid_search import (
    PostgresFTSHybridService,
    _reciprocal_rank_fusion,
)


def _make_chunk(chunk_id: str, text: str) -> Any:
    return SimpleNamespace(
        id=chunk_id,
        text=text,
        search_metadata={},
        source_uri=None,
        tags=[],
    )


@pytest.mark.unit
def test_rrf_fuses_overlapping_results() -> None:
    """A document that ranks high in both lists wins."""
    a, b, c = "a", "b", "c"
    vector = [
        {"id": a, "text": "a", "metadata": {}, "source_uri": None, "tags": [], "similarity": 0.95},
        {"id": b, "text": "b", "metadata": {}, "source_uri": None, "tags": [], "similarity": 0.85},
    ]
    bm25 = [
        {"id": a, "text": "a", "bm25_score": 12.0, "metadata": {}},
        {"id": c, "text": "c", "bm25_score": 8.0, "metadata": {}},
    ]
    fused = _reciprocal_rank_fusion(vector, bm25, bm25_weight=0.5, semantic_weight=0.5)
    ids = [r["id"] for r in fused]
    assert ids[0] == a, "doc present in both lists should fuse to top"
    assert set(ids) == {a, b, c}


@pytest.mark.unit
def test_rrf_pulls_in_bm25_only_match() -> None:
    """A doc that vector retrieval missed but BM25 found still surfaces."""
    bm25_only = "code-error-7331"
    vector = [
        {"id": "x", "text": "x", "metadata": {}, "source_uri": None, "tags": [], "similarity": 0.9},
    ]
    bm25 = [
        {"id": bm25_only, "text": "...7331...", "bm25_score": 9.5, "metadata": {}},
    ]
    fused = _reciprocal_rank_fusion(vector, bm25, bm25_weight=0.5, semantic_weight=0.5)
    assert bm25_only in [r["id"] for r in fused]


@pytest.mark.unit
def test_rrf_weighting_biases_winner() -> None:
    """Heavy bm25_weight makes the BM25-only match outrank the vector-only match."""
    vector = [
        {"id": "v", "text": "v", "metadata": {}, "source_uri": None, "tags": [], "similarity": 0.9},
    ]
    bm25 = [
        {"id": "k", "text": "k", "bm25_score": 5.0, "metadata": {}},
    ]
    fused = _reciprocal_rank_fusion(vector, bm25, bm25_weight=0.95, semantic_weight=0.05)
    assert fused[0]["id"] == "k"

    fused = _reciprocal_rank_fusion(vector, bm25, bm25_weight=0.05, semantic_weight=0.95)
    assert fused[0]["id"] == "v"


@pytest.mark.asyncio
async def test_search_runs_bm25_and_vector_in_parallel() -> None:
    """``search`` calls both retrievers and merges via RRF."""
    chunk_a_id = uuid4()
    chunk_b_id = uuid4()

    repo = MagicMock()
    repo.session = MagicMock()
    repo.search_by_embedding = AsyncMock(return_value=[(_make_chunk(chunk_a_id, "alpha"), 0.9)])

    embeddings = MagicMock()
    embeddings.embed_text = AsyncMock(return_value=[0.0] * 384)

    bm25 = MagicMock()
    bm25.search = AsyncMock(
        return_value=[{"id": str(chunk_b_id), "text": "beta", "bm25_score": 12.0, "metadata": {}}]
    )

    service = PostgresFTSHybridService(
        repository=repo,
        embeddings=embeddings,
        bm25_service=bm25,
    )

    user_context = SimpleNamespace(tenant_id=str(uuid4()))
    results = await service.search(
        query="alpha beta",
        user_context=user_context,
        top_k=5,
    )

    embeddings.embed_text.assert_awaited_once()
    repo.search_by_embedding.assert_awaited_once()
    bm25.search.assert_awaited_once()

    ids = {r["id"] for r in results}
    assert ids == {str(chunk_a_id), str(chunk_b_id)}
