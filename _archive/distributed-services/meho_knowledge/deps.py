"""
FastAPI dependencies for Knowledge Service.

Provides dependency injection for routes.
"""
# mypy: disable-error-code="no-untyped-def"
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from pathlib import Path
from meho_knowledge.database import get_session
from meho_knowledge.repository import KnowledgeRepository
from meho_knowledge.embeddings import get_embedding_provider, EmbeddingProvider
from meho_knowledge.knowledge_store import KnowledgeStore
from meho_knowledge.ingestion import IngestionService
from meho_knowledge.object_storage import ObjectStorage
from meho_knowledge.hybrid_search import PostgresFTSHybridService


# Singleton instances
_object_storage = None


def get_object_storage() -> ObjectStorage:
    """Get object storage singleton"""
    global _object_storage
    
    if _object_storage is None:
        _object_storage = ObjectStorage()
    
    return _object_storage




async def get_repository(
    session: AsyncSession = Depends(get_session)
) -> KnowledgeRepository:
    """Get repository with database session"""
    return KnowledgeRepository(session)


async def get_knowledge_store(
    repository: KnowledgeRepository = Depends(get_repository),
    embedding_provider: EmbeddingProvider = Depends(get_embedding_provider)
) -> KnowledgeStore:
    """Get knowledge store (PostgreSQL with pgvector + PostgreSQL FTS hybrid search)"""
    # Create hybrid search service (uses PostgreSQL FTS instead of BM25 pickle files)
    hybrid_search = PostgresFTSHybridService(repository, embedding_provider)
    return KnowledgeStore(repository, embedding_provider, hybrid_search)


async def get_job_repository(
    session: AsyncSession = Depends(get_session)
):
    """Get ingestion job repository"""
    from meho_knowledge.job_repository import IngestionJobRepository
    return IngestionJobRepository(session)


async def get_hybrid_search(
    repository: KnowledgeRepository = Depends(get_repository),
    embedding_provider: EmbeddingProvider = Depends(get_embedding_provider)
) -> PostgresFTSHybridService:
    """Get hybrid search service (PostgreSQL FTS + semantic)"""
    return PostgresFTSHybridService(repository, embedding_provider)


async def get_ingestion_service(
    knowledge_store: KnowledgeStore = Depends(get_knowledge_store),
    object_storage: ObjectStorage = Depends(get_object_storage),
    job_repository = Depends(get_job_repository)
) -> IngestionService:
    """Get ingestion service with dependencies"""
    # No bm25_manager needed - PostgreSQL FTS indexes are maintained automatically
    return IngestionService(knowledge_store, object_storage, job_repository)

