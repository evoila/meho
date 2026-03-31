# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Embedding service for topology entities.

Generates embeddings for entity descriptions to enable:
1. Semantic search of entities
2. Cross-connector correlation discovery (SAME_AS)

Reuses the Voyage AI embedding infrastructure from the knowledge module.
"""

from meho_app.core.otel import get_logger
from meho_app.modules.knowledge.embeddings import (
    EmbeddingProvider,
    get_embedding_provider,
)

logger = get_logger(__name__)


class TopologyEmbeddingService:
    """
    Service for generating embeddings for topology entities.

    Uses the same embedding model as the knowledge module (Voyage AI voyage-4-large, 1024D)
    to ensure consistent semantic similarity across the system.

    Usage:
        service = TopologyEmbeddingService()
        embedding = await service.generate_embedding(
            "Kubernetes Node node-01, IP 192.168.1.10, cluster prod-k8s"
        )
    """

    def __init__(self, embedding_provider: EmbeddingProvider | None = None):
        """
        Initialize the embedding service.

        Args:
            embedding_provider: Optional provider override (for testing).
                              If not provided, uses the default Voyage AI provider.
        """
        self._provider = embedding_provider

    @property
    def provider(self) -> EmbeddingProvider:
        """Get the embedding provider (lazy initialization)."""
        if self._provider is None:
            self._provider = get_embedding_provider()
        return self._provider

    async def generate_embedding(self, text: str) -> list[float]:
        """
        Generate an embedding for text.

        Args:
            text: Text to embed (typically entity description)

        Returns:
            Embedding vector as list of floats (1024 dimensions)

        Example:
            text = "Kubernetes Node node-01, IP 192.168.1.10, cluster prod-k8s, labels: role=worker, env=prod"
            embedding = await service.generate_embedding(text)
        """
        logger.debug(f"Generating embedding for text: {text[:100]}...")
        embedding = await self.provider.embed_text(text)
        logger.debug(f"Generated embedding with {len(embedding)} dimensions")
        return embedding

    async def generate_embeddings_batch(self, texts: list[str]) -> list[list[float]]:
        """
        Generate embeddings for multiple texts in batch.

        More efficient than calling generate_embedding multiple times.

        Args:
            texts: List of texts to embed

        Returns:
            List of embedding vectors
        """
        if not texts:
            return []

        logger.debug(f"Generating embeddings for {len(texts)} texts")
        embeddings = await self.provider.embed_batch(texts)
        logger.debug(f"Generated {len(embeddings)} embeddings")
        return embeddings


# =============================================================================
# Singleton / Dependency Injection
# =============================================================================

_embedding_service: TopologyEmbeddingService | None = None


def get_topology_embedding_service() -> TopologyEmbeddingService:
    """
    Get the topology embedding service singleton.

    Returns:
        Configured embedding service
    """
    global _embedding_service

    if _embedding_service is None:
        _embedding_service = TopologyEmbeddingService()

    return _embedding_service


def reset_topology_embedding_service() -> None:
    """Reset the embedding service singleton (for testing)."""
    global _embedding_service
    _embedding_service = None
