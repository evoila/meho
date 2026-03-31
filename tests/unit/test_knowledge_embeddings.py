# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for meho_app/modules/knowledge/embeddings.py

Tests the embedding provider abstraction and Voyage AI integration.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_app.modules.knowledge.embeddings import (
    VoyageAIEmbeddings,
    get_embedding_provider,
    reset_embedding_provider,
)


class TestVoyageAIEmbeddings:
    """Tests for VoyageAIEmbeddings class"""

    def test_init_default_model(self):
        """Test initialization with default model"""
        with patch("voyageai.AsyncClient"):
            embeddings = VoyageAIEmbeddings(api_key="test-key")

        assert embeddings.model == "voyage-4-large"
        assert embeddings.dimension == 1024
        assert embeddings.client is not None

    def test_init_custom_model(self):
        """Test initialization with custom model"""
        with patch("voyageai.AsyncClient"):
            embeddings = VoyageAIEmbeddings(api_key="test-key", model="voyage-3-lite")

        assert embeddings.model == "voyage-3-lite"
        assert embeddings.dimension == 1024

    @pytest.mark.asyncio
    async def test_embed_text_success(self):
        """Test successful single text embedding"""
        with patch("voyageai.AsyncClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client

            embeddings = VoyageAIEmbeddings(api_key="test-key")

            # Mock the Voyage AI response
            mock_result = MagicMock()
            mock_result.embeddings = [[0.1, 0.2, 0.3, 0.4, 0.5]]
            mock_client.embed = AsyncMock(return_value=mock_result)

            result = await embeddings.embed_text("test text")

            assert result == [0.1, 0.2, 0.3, 0.4, 0.5]
            mock_client.embed.assert_called_once_with(
                ["test text"],
                model="voyage-4-large",
                input_type="query",
            )

    @pytest.mark.asyncio
    async def test_embed_text_with_document_input_type(self):
        """Test embedding with document input_type"""
        with patch("voyageai.AsyncClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client

            embeddings = VoyageAIEmbeddings(api_key="test-key")

            mock_result = MagicMock()
            mock_result.embeddings = [[0.1, 0.2, 0.3]]
            mock_client.embed = AsyncMock(return_value=mock_result)

            result = await embeddings.embed_text("document text", input_type="document")

            assert result == [0.1, 0.2, 0.3]
            mock_client.embed.assert_called_once_with(
                ["document text"],
                model="voyage-4-large",
                input_type="document",
            )

    @pytest.mark.asyncio
    async def test_embed_batch_success(self):
        """Test successful batch embedding"""
        with patch("voyageai.AsyncClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client

            embeddings = VoyageAIEmbeddings(api_key="test-key")

            mock_result = MagicMock()
            mock_result.embeddings = [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]]
            mock_client.embed = AsyncMock(return_value=mock_result)

            texts = ["text 1", "text 2", "text 3"]
            result = await embeddings.embed_batch(texts)

            assert result == [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]]
            mock_client.embed.assert_called_once_with(
                texts,
                model="voyage-4-large",
                input_type="document",
            )

    @pytest.mark.asyncio
    async def test_embed_batch_empty_list(self):
        """Test batch embedding with empty list returns empty"""
        with patch("voyageai.AsyncClient"):
            embeddings = VoyageAIEmbeddings(api_key="test-key")

        result = await embeddings.embed_batch([])

        assert result == []

    @pytest.mark.asyncio
    async def test_embed_batch_chunking(self):
        """Test batch embedding chunks at 1000 items"""
        with patch("voyageai.AsyncClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client

            embeddings = VoyageAIEmbeddings(api_key="test-key")

            # Create 1500 texts (should be split into 1000 + 500)
            texts = [f"text {i}" for i in range(1500)]

            mock_result_1 = MagicMock()
            mock_result_1.embeddings = [[float(i)] for i in range(1000)]
            mock_result_2 = MagicMock()
            mock_result_2.embeddings = [[float(i)] for i in range(1000, 1500)]
            mock_client.embed = AsyncMock(side_effect=[mock_result_1, mock_result_2])

            result = await embeddings.embed_batch(texts)

            assert len(result) == 1500
            assert mock_client.embed.call_count == 2


class TestEmbeddingProviderSingleton:
    """Tests for embedding provider singleton management"""

    def setup_method(self):
        """Reset singleton before each test"""
        reset_embedding_provider()

    def teardown_method(self):
        """Reset singleton after each test"""
        reset_embedding_provider()

    @patch("meho_app.modules.knowledge.embeddings.get_config")
    def test_get_embedding_provider_returns_voyage_ai(self, mock_get_config):
        """Test getting embedding provider returns VoyageAIEmbeddings"""
        mock_config = MagicMock()
        mock_config.voyage_api_key = "test-voyage-key"
        mock_config.embedding_model = "voyage-4-large"
        mock_get_config.return_value = mock_config

        with patch("voyageai.AsyncClient"):
            provider = get_embedding_provider()

        assert provider is not None
        assert isinstance(provider, VoyageAIEmbeddings)
        assert provider.model == "voyage-4-large"
        mock_get_config.assert_called_once()

    @patch("meho_app.modules.knowledge.embeddings.get_config")
    def test_get_embedding_provider_subsequent_calls(self, mock_get_config):
        """Test getting embedding provider on subsequent calls (returns same singleton)"""
        mock_config = MagicMock()
        mock_config.voyage_api_key = "test-voyage-key"
        mock_config.embedding_model = "voyage-4-large"
        mock_get_config.return_value = mock_config

        with patch("voyageai.AsyncClient"):
            provider1 = get_embedding_provider()
            provider2 = get_embedding_provider()
            provider3 = get_embedding_provider()

        assert provider1 is provider2
        assert provider2 is provider3
        assert mock_get_config.call_count == 1

    @patch("meho_app.modules.knowledge.embeddings.get_config")
    def test_reset_embedding_provider(self, mock_get_config):
        """Test resetting the embedding provider singleton"""
        mock_config = MagicMock()
        mock_config.voyage_api_key = "test-voyage-key"
        mock_config.embedding_model = "voyage-4-large"
        mock_get_config.return_value = mock_config

        with patch("voyageai.AsyncClient"):
            provider1 = get_embedding_provider()
            reset_embedding_provider()
            provider2 = get_embedding_provider()

        assert provider1 is not provider2
        assert mock_get_config.call_count == 2
