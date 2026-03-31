# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for TEIEmbeddings class and factory selection logic.

Tests the local TEI embedding provider and the config-driven factory
that selects between Voyage AI (enterprise) and TEI (community) providers.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


class TestTEIEmbeddings:
    """Tests for TEIEmbeddings class"""

    def test_init_default_url(self):
        """Test initialization with default URL"""
        from meho_app.modules.knowledge.embeddings import TEIEmbeddings

        tei = TEIEmbeddings()
        assert tei.base_url == "http://tei-embeddings:80"
        assert tei.dimension == 1024
        assert tei._batch_size == 32

    def test_init_custom_url(self):
        """Test initialization with custom URL"""
        from meho_app.modules.knowledge.embeddings import TEIEmbeddings

        tei = TEIEmbeddings(base_url="http://custom-tei:9090/")
        assert tei.base_url == "http://custom-tei:9090"  # Trailing slash stripped

    @pytest.mark.asyncio
    async def test_embed_text_success(self):
        """Test TEIEmbeddings.embed_text POSTs to /embed with correct payload"""
        from meho_app.modules.knowledge.embeddings import TEIEmbeddings

        tei = TEIEmbeddings(base_url="http://test:80")
        mock_response = MagicMock()
        mock_response.json.return_value = [[0.1, 0.2, 0.3] + [0.0] * 1021]
        mock_response.raise_for_status = MagicMock()

        mock_post = AsyncMock(return_value=mock_response)
        with patch.object(tei.client, "post", mock_post):
            result = await tei.embed_text("hello")

        assert len(result) == 1024
        assert result[0] == 0.1
        mock_post.assert_called_once_with(
            "/embed",
            json={"inputs": "hello", "normalize": True, "truncate": True},
        )

    @pytest.mark.asyncio
    async def test_embed_text_accepts_input_type_kwarg(self):
        """Test TEIEmbeddings.embed_text accepts input_type kwarg without TypeError"""
        from meho_app.modules.knowledge.embeddings import TEIEmbeddings

        tei = TEIEmbeddings(base_url="http://test:80")
        mock_response = MagicMock()
        mock_response.json.return_value = [[0.1] * 1024]
        mock_response.raise_for_status = MagicMock()

        mock_post = AsyncMock(return_value=mock_response)
        with patch.object(tei.client, "post", mock_post):
            # This must NOT raise TypeError -- input_type is Voyage-specific, absorbed via **kwargs
            result = await tei.embed_text("hello", input_type="document")

        assert len(result) == 1024

    @pytest.mark.asyncio
    async def test_embed_batch_success(self):
        """Test TEIEmbeddings.embed_batch returns list of embedding vectors"""
        from meho_app.modules.knowledge.embeddings import TEIEmbeddings

        tei = TEIEmbeddings(base_url="http://test:80")
        mock_response = MagicMock()
        mock_response.json.return_value = [[0.1] * 1024, [0.2] * 1024]
        mock_response.raise_for_status = MagicMock()

        mock_post = AsyncMock(return_value=mock_response)
        with patch.object(tei.client, "post", mock_post):
            result = await tei.embed_batch(["a", "b"])

        assert len(result) == 2
        assert len(result[0]) == 1024
        assert len(result[1]) == 1024

    @pytest.mark.asyncio
    async def test_embed_batch_empty_returns_empty(self):
        """Test TEIEmbeddings.embed_batch([]) returns []"""
        from meho_app.modules.knowledge.embeddings import TEIEmbeddings

        tei = TEIEmbeddings(base_url="http://test:80")
        result = await tei.embed_batch([])
        assert result == []

    @pytest.mark.asyncio
    async def test_embed_batch_chunks_at_32(self):
        """Test TEIEmbeddings.embed_batch chunks into batches of 32"""
        from meho_app.modules.knowledge.embeddings import TEIEmbeddings

        tei = TEIEmbeddings(base_url="http://test:80")

        # 50 items should result in 2 calls: 32 + 18
        texts = [f"text {i}" for i in range(50)]

        mock_response_1 = MagicMock()
        mock_response_1.json.return_value = [[0.1] * 1024] * 32
        mock_response_1.raise_for_status = MagicMock()

        mock_response_2 = MagicMock()
        mock_response_2.json.return_value = [[0.2] * 1024] * 18
        mock_response_2.raise_for_status = MagicMock()

        mock_post = AsyncMock(side_effect=[mock_response_1, mock_response_2])
        with patch.object(tei.client, "post", mock_post):
            result = await tei.embed_batch(texts)

        assert len(result) == 50
        assert mock_post.call_count == 2

    def test_dimension_is_1024(self):
        """Test TEIEmbeddings.dimension == 1024"""
        from meho_app.modules.knowledge.embeddings import TEIEmbeddings

        tei = TEIEmbeddings()
        assert tei.dimension == 1024


class TestEmbeddingFactorySelection:
    """Tests for factory selection between TEI and Voyage AI"""

    def setup_method(self):
        from meho_app.modules.knowledge.embeddings import reset_embedding_provider

        reset_embedding_provider()

    def teardown_method(self):
        from meho_app.modules.knowledge.embeddings import reset_embedding_provider

        reset_embedding_provider()

    @patch("meho_app.modules.knowledge.embeddings.get_config")
    def test_factory_returns_tei_when_no_voyage_key(self, mock_get_config):
        """get_embedding_provider() returns TEIEmbeddings when config.voyage_api_key is None"""
        from meho_app.modules.knowledge.embeddings import TEIEmbeddings, get_embedding_provider

        mock_config = MagicMock()
        mock_config.voyage_api_key = None
        mock_config.tei_embedding_url = "http://tei-embeddings:80"
        mock_get_config.return_value = mock_config

        provider = get_embedding_provider()
        assert isinstance(provider, TEIEmbeddings)
        assert provider.base_url == "http://tei-embeddings:80"

    @patch("meho_app.modules.knowledge.embeddings.get_config")
    def test_factory_returns_voyage_when_key_present(self, mock_get_config):
        """get_embedding_provider() returns VoyageAIEmbeddings when config.voyage_api_key is set"""
        from meho_app.modules.knowledge.embeddings import (
            VoyageAIEmbeddings,
            get_embedding_provider,
        )

        mock_config = MagicMock()
        mock_config.voyage_api_key = "test-voyage-key"
        mock_config.embedding_model = "voyage-4-large"
        mock_get_config.return_value = mock_config

        with patch("voyageai.AsyncClient"):
            provider = get_embedding_provider()

        assert isinstance(provider, VoyageAIEmbeddings)
