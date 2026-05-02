# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
FastAPI dependencies for Ingestion module.
"""

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.database import get_db_session
from meho_app.modules.ingestion.processor import GenericWebhookProcessor
from meho_app.modules.ingestion.repository import EventTemplateRepository
from meho_app.modules.knowledge.embeddings import get_embedding_provider
from meho_app.modules.knowledge.knowledge_store import KnowledgeStore
from meho_app.modules.knowledge.repository import KnowledgeRepository


def get_template_repository(
    session: AsyncSession = Depends(get_db_session),
) -> EventTemplateRepository:
    """Get event template repository."""
    return EventTemplateRepository(session)


def get_webhook_processor(
    session: AsyncSession = Depends(get_db_session),
) -> GenericWebhookProcessor:
    """Get webhook processor."""
    template_repo = EventTemplateRepository(session)
    knowledge_repo = KnowledgeRepository(session)
    embedding_provider = get_embedding_provider()
    knowledge_store = KnowledgeStore(
        repository=knowledge_repo,
        embedding_provider=embedding_provider,
    )
    return GenericWebhookProcessor(template_repo, knowledge_store)
