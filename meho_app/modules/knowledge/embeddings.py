# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Embedding generation for knowledge chunks.

Provides embedding providers for knowledge base and topology:
- VoyageAIEmbeddings: Enterprise mode (when VOYAGE_API_KEY is set)
- TEIEmbeddings: Community mode (local TEI sidecar, default when no Voyage key)
"""

from typing import Protocol

import httpx

from meho_app.core.config import get_config


class EmbeddingProvider(Protocol):
    """Protocol for embedding providers"""

    async def embed_text(self, text: str) -> list[float]:
        """
        Generate embedding vector for text.

        Args:
            text: Text to embed

        Returns:
            Embedding vector as list of floats
        """
        ...

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """
        Generate embeddings for multiple texts (batch operation).

        Args:
            texts: List of texts to embed

        Returns:
            List of embedding vectors
        """
        ...


class VoyageAIEmbeddings:
    """Voyage AI embedding provider with input_type support for better retrieval."""

    def __init__(self, api_key: str, model: str = "voyage-4-large"):
        import voyageai  # Lazy import -- not needed in community mode

        self.client = voyageai.AsyncClient(api_key=api_key)
        self.model = model
        self.dimension = 1024  # Voyage 4 models default to 1024D

    async def embed_text(self, text: str, input_type: str = "query") -> list[float]:
        """Generate embedding for single text.

        Args:
            text: Text to embed
            input_type: "query" for search queries, "document" for indexing
        """
        result = await self.client.embed(
            [text],
            model=self.model,
            input_type=input_type,
        )
        return result.embeddings[0]

    async def embed_batch(
        self, texts: list[str], input_type: str = "document"
    ) -> list[list[float]]:
        """Generate embeddings for multiple texts.

        Voyage AI supports max 1000 items per request.
        Defaults to input_type="document" for batch operations (typically indexing).
        """
        if not texts:
            return []
        all_embeddings: list[list[float]] = []
        # Voyage AI max batch size is 1000
        for i in range(0, len(texts), 1000):
            batch = texts[i : i + 1000]
            result = await self.client.embed(
                batch,
                model=self.model,
                input_type=input_type,
            )
            all_embeddings.extend(result.embeddings)
        return all_embeddings


class TEIEmbeddings:
    """Local TEI embedding provider using bge-m3 via HTTP."""

    def __init__(self, base_url: str = "http://tei-embeddings:80"):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(base_url=self.base_url, timeout=30.0)
        self.dimension = 1024  # bge-m3 produces 1024D vectors (matches Voyage AI)
        self._batch_size = 32  # TEI default --max-client-batch-size

    async def embed_text(self, text: str, **kwargs) -> list[float]:
        """Generate embedding for single text.

        input_type kwarg is accepted but ignored (Voyage-specific concept).
        bge-m3 does not distinguish query vs document embeddings.
        """
        response = await self.client.post(
            "/embed",
            json={"inputs": text, "normalize": True, "truncate": True},
        )
        response.raise_for_status()
        return response.json()[0]

    async def embed_batch(self, texts: list[str], **kwargs) -> list[list[float]]:
        """Generate embeddings for batch.

        Chunks at 32 items (TEI default max-client-batch-size).
        input_type kwarg is accepted but ignored.
        """
        if not texts:
            return []
        all_embeddings: list[list[float]] = []
        for i in range(0, len(texts), self._batch_size):
            batch = texts[i : i + self._batch_size]
            response = await self.client.post(
                "/embed",
                json={"inputs": batch, "normalize": True, "truncate": True},
            )
            response.raise_for_status()
            all_embeddings.extend(response.json())
        return all_embeddings


# Singleton instance
_embedding_provider: EmbeddingProvider | None = None


def get_embedding_provider() -> EmbeddingProvider:
    """
    Get embedding provider singleton.

    TEI when no Voyage key (community mode), Voyage AI when key present (enterprise).
    """
    global _embedding_provider

    if _embedding_provider is None:
        config = get_config()
        if config.voyage_api_key:
            _embedding_provider = VoyageAIEmbeddings(
                api_key=config.voyage_api_key, model=config.embedding_model
            )
        else:
            _embedding_provider = TEIEmbeddings(base_url=config.tei_embedding_url)

    return _embedding_provider


def reset_embedding_provider() -> None:
    """Reset embedding provider singleton (for testing)"""
    global _embedding_provider
    _embedding_provider = None
