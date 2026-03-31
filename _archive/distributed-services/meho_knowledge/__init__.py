"""
MEHO Knowledge Service - Knowledge management with RAG and ACL using pgvector.

Exports:
    - Models: KnowledgeChunkModel
    - Schemas: KnowledgeChunkCreate, KnowledgeChunk, KnowledgeChunkFilter
    - Repository: KnowledgeRepository (now handles vector search with pgvector)
    - Embeddings: OpenAIEmbeddings, get_embedding_provider
    - Knowledge Store: KnowledgeStore (unified interface)
"""
from meho_knowledge.models import KnowledgeChunkModel, Base
from meho_knowledge.schemas import KnowledgeChunkCreate, KnowledgeChunk, KnowledgeChunkFilter
from meho_knowledge.repository import KnowledgeRepository
from meho_knowledge.embeddings import OpenAIEmbeddings, get_embedding_provider, reset_embedding_provider
from meho_knowledge.knowledge_store import KnowledgeStore

__all__ = [
    "KnowledgeChunkModel",
    "Base",
    "KnowledgeChunkCreate",
    "KnowledgeChunk",
    "KnowledgeChunkFilter",
    "KnowledgeRepository",
    "OpenAIEmbeddings",
    "get_embedding_provider",
    "reset_embedding_provider",
    "KnowledgeStore",
]

