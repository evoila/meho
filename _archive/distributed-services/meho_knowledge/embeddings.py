"""
Embedding generation for knowledge chunks.

Provides abstraction over embedding providers (OpenAI, etc.)
"""
from typing import List, Protocol
from openai import AsyncOpenAI
from meho_core.config import get_config


class EmbeddingProvider(Protocol):
    """Protocol for embedding providers"""
    
    async def embed_text(self, text: str) -> List[float]:
        """
        Generate embedding vector for text.
        
        Args:
            text: Text to embed
        
        Returns:
            Embedding vector as list of floats
        """
        ...
    
    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """
        Generate embeddings for multiple texts (batch operation).
        
        Args:
            texts: List of texts to embed
        
        Returns:
            List of embedding vectors
        """
        ...


class OpenAIEmbeddings:
    """OpenAI embedding provider"""
    
    def __init__(self, api_key: str, model: str = "text-embedding-3-small"):
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model
        # Dimensions based on model
        # text-embedding-3-small: 1536 dimensions (fits HNSW 2000D limit)
        # text-embedding-3-large: 3072 dimensions (requires IVFFlat - see docs/PGVECTOR-INDEX-COMPARISON.md)
        self.dimension = 1536 if "small" in model else 3072
    
    async def embed_text(self, text: str) -> List[float]:
        """Generate embedding for single text"""
        response = await self.client.embeddings.create(
            model=self.model,
            input=text
        )
        return response.data[0].embedding
    
    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings for multiple texts"""
        response = await self.client.embeddings.create(
            model=self.model,
            input=texts
        )
        return [item.embedding for item in response.data]


# Singleton instance
_embedding_provider: OpenAIEmbeddings | None = None


def get_embedding_provider() -> OpenAIEmbeddings:
    """
    Get embedding provider singleton.
    
    Returns:
        Configured embedding provider
    """
    global _embedding_provider
    
    if _embedding_provider is None:
        config = get_config()
        _embedding_provider = OpenAIEmbeddings(
            api_key=config.openai_api_key,
            model=config.embedding_model
        )
    
    return _embedding_provider


def reset_embedding_provider() -> None:
    """Reset embedding provider singleton (for testing)"""
    global _embedding_provider
    _embedding_provider = None

