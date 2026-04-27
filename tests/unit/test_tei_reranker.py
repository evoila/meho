# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for TEIReranker class and reranker factory selection logic.

Tests the local TEI reranker provider, RerankerProvider protocol,
and the config-driven factory that selects between Voyage AI and TEI rerankers.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestTEIReranker:
    """Tests for TEIReranker class"""

    @pytest.mark.asyncio
    async def test_rerank_success(self):
        """TEIReranker.rerank POSTs to /rerank and returns correct format"""
        from meho_app.modules.knowledge.reranker import TEIReranker

        reranker = TEIReranker(base_url="http://test:80")
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {"index": 1, "score": 0.95},
            {"index": 0, "score": 0.42},
        ]
        mock_response.raise_for_status = MagicMock()

        with patch.object(
            reranker.client, "post", new_callable=AsyncMock, return_value=mock_response
        ):
            result = await reranker.rerank(
                query="What is deep learning?",
                documents=["Deep learning is...", "Cooking recipes..."],
                top_k=2,
            )

        assert len(result) == 2
        assert result[0]["index"] == 1
        assert result[0]["relevance_score"] == pytest.approx(0.95)
        assert result[0]["document"] == "Cooking recipes..."
        assert result[1]["index"] == 0
        assert result[1]["relevance_score"] == pytest.approx(0.42)
        assert result[1]["document"] == "Deep learning is..."

    @pytest.mark.asyncio
    async def test_rerank_empty_documents(self):
        """TEIReranker.rerank with empty documents returns []"""
        from meho_app.modules.knowledge.reranker import TEIReranker

        reranker = TEIReranker(base_url="http://test:80")
        result = await reranker.rerank(query="test", documents=[], top_k=5)
        assert result == []

    @pytest.mark.asyncio
    async def test_rerank_single_document(self):
        """TEIReranker.rerank with single document returns it with score 1.0"""
        from meho_app.modules.knowledge.reranker import TEIReranker

        reranker = TEIReranker(base_url="http://test:80")
        result = await reranker.rerank(query="test", documents=["only doc"], top_k=1)
        assert len(result) == 1
        assert result[0]["index"] == 0
        assert result[0]["relevance_score"] == pytest.approx(1.0)
        assert result[0]["document"] == "only doc"

    @pytest.mark.asyncio
    async def test_rerank_falls_back_on_error(self):
        """TEIReranker.rerank falls back to original order on error"""
        from meho_app.modules.knowledge.reranker import TEIReranker

        reranker = TEIReranker(base_url="http://test:80")

        with patch.object(
            reranker.client,
            "post",
            new_callable=AsyncMock,
            side_effect=Exception("Connection refused"),
        ):
            result = await reranker.rerank(
                query="test",
                documents=["doc A", "doc B", "doc C"],
                top_k=3,
            )

        assert len(result) == 3
        assert result[0]["index"] == 0
        assert result[0]["relevance_score"] == pytest.approx(0.0)
        assert result[0]["document"] == "doc A"

    @pytest.mark.asyncio
    async def test_rerank_respects_top_k(self):
        """TEIReranker.rerank returns at most top_k results"""
        from meho_app.modules.knowledge.reranker import TEIReranker

        reranker = TEIReranker(base_url="http://test:80")
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {"index": 0, "score": 0.9},
            {"index": 1, "score": 0.8},
            {"index": 2, "score": 0.7},
        ]
        mock_response.raise_for_status = MagicMock()

        with patch.object(
            reranker.client, "post", new_callable=AsyncMock, return_value=mock_response
        ):
            result = await reranker.rerank(
                query="test",
                documents=["doc A", "doc B", "doc C"],
                top_k=2,
            )

        assert len(result) == 2


class TestRerankerProtocol:
    """Tests for RerankerProvider protocol conformance"""

    def test_voyage_reranker_satisfies_protocol(self):
        """VoyageReranker satisfies RerankerProvider protocol"""
        from meho_app.modules.knowledge.reranker import RerankerProvider, VoyageReranker

        with patch("voyageai.AsyncClient"):
            reranker = VoyageReranker(api_key="test-key")

        # Protocol check: isinstance works with runtime_checkable
        assert isinstance(reranker, RerankerProvider)

    def test_tei_reranker_satisfies_protocol(self):
        """TEIReranker satisfies RerankerProvider protocol"""
        from meho_app.modules.knowledge.reranker import RerankerProvider, TEIReranker

        reranker = TEIReranker(base_url="http://test:80")
        assert isinstance(reranker, RerankerProvider)


class TestRerankerFactorySelection:
    """Tests for reranker factory selection"""

    def setup_method(self):
        from meho_app.modules.knowledge.reranker import reset_reranker

        reset_reranker()

    def teardown_method(self):
        from meho_app.modules.knowledge.reranker import reset_reranker

        reset_reranker()

    @patch("meho_app.core.config.get_config")
    def test_factory_returns_tei_when_no_voyage_key(self, mock_get_config):
        """get_reranker() returns TEIReranker when config.voyage_api_key is None"""
        from meho_app.modules.knowledge.reranker import TEIReranker, get_reranker

        mock_config = MagicMock()
        mock_config.voyage_api_key = None
        mock_config.tei_reranker_url = "http://tei-reranker:80"
        mock_get_config.return_value = mock_config

        reranker = get_reranker()
        assert isinstance(reranker, TEIReranker)

    @patch("meho_app.core.config.get_config")
    def test_factory_returns_voyage_when_key_present(self, mock_get_config):
        """get_reranker() returns VoyageReranker when config.voyage_api_key is set"""
        from meho_app.modules.knowledge.reranker import VoyageReranker, get_reranker

        mock_config = MagicMock()
        mock_config.voyage_api_key = "test-voyage-key"
        mock_get_config.return_value = mock_config

        with patch("voyageai.AsyncClient"):
            reranker = get_reranker()

        assert isinstance(reranker, VoyageReranker)
