# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
FastAPI dependencies for Knowledge module.

Provides dependency injection for routes.
"""

from typing import Any

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.database import get_db_session
from meho_app.modules.knowledge.embeddings import EmbeddingProvider, get_embedding_provider
from meho_app.modules.knowledge.hybrid_search import PostgresFTSHybridService
from meho_app.modules.knowledge.ingestion import IngestionService
from meho_app.modules.knowledge.knowledge_store import KnowledgeStore
from meho_app.modules.knowledge.object_storage import ObjectStorage
from meho_app.modules.knowledge.repository import KnowledgeRepository

# Singleton instances
_object_storage = None


def get_object_storage() -> ObjectStorage:
    """Get object storage singleton"""
    global _object_storage

    if _object_storage is None:
        _object_storage = ObjectStorage()

    return _object_storage


def get_repository(session: AsyncSession = Depends(get_db_session)) -> KnowledgeRepository:
    """Get repository with database session"""
    return KnowledgeRepository(session)


def get_knowledge_store(
    repository: KnowledgeRepository = Depends(get_repository),
    embedding_provider: EmbeddingProvider = Depends(get_embedding_provider),
) -> KnowledgeStore:
    """Get knowledge store (PostgreSQL with pgvector + PostgreSQL FTS hybrid search)"""
    # Create hybrid search service (uses PostgreSQL FTS instead of BM25 pickle files)
    hybrid_search = PostgresFTSHybridService(repository, embedding_provider)
    return KnowledgeStore(repository, embedding_provider, hybrid_search)


def get_job_repository(session: AsyncSession = Depends(get_db_session)) -> Any:
    """Get ingestion job repository"""
    from meho_app.modules.knowledge.job_repository import IngestionJobRepository

    return IngestionJobRepository(session)


def get_hybrid_search(
    repository: KnowledgeRepository = Depends(get_repository),
    embedding_provider: EmbeddingProvider = Depends(get_embedding_provider),
) -> PostgresFTSHybridService:
    """Get hybrid search service (PostgreSQL FTS + semantic)"""
    return PostgresFTSHybridService(repository, embedding_provider)


def get_ingestion_service(
    knowledge_store: KnowledgeStore = Depends(get_knowledge_store),
    object_storage: ObjectStorage = Depends(get_object_storage),
    job_repository: Any = Depends(get_job_repository),
) -> IngestionService:
    """Get ingestion service with dependencies"""
    # No bm25_manager needed - PostgreSQL FTS indexes are maintained automatically
    return IngestionService(knowledge_store, object_storage, job_repository)
