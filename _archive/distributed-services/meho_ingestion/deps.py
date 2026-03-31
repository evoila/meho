"""
Dependency injection for Ingestion Service.
"""
from meho_knowledge.deps import get_knowledge_store
from meho_knowledge.database import get_session
from meho_knowledge.knowledge_store import KnowledgeStore
from meho_ingestion.repository import EventTemplateRepository
from meho_ingestion.template_renderer import TemplateRenderer
from meho_ingestion.processor import GenericWebhookProcessor
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends
from functools import lru_cache


# Re-export for convenience
__all__ = [
    "get_knowledge_store",
    "get_session",
    "get_template_repository",
    "get_template_renderer",
    "get_webhook_processor"
]


async def get_template_repository(
    session: AsyncSession = Depends(get_session)
) -> EventTemplateRepository:
    """Get event template repository"""
    return EventTemplateRepository(session)


@lru_cache()
def get_template_renderer() -> TemplateRenderer:
    """Get template renderer (cached singleton)"""
    return TemplateRenderer()


def get_webhook_processor(
    template_repo: EventTemplateRepository = Depends(get_template_repository),
    knowledge_store: KnowledgeStore = Depends(get_knowledge_store),
    renderer: TemplateRenderer = Depends(get_template_renderer)
) -> GenericWebhookProcessor:
    """Get generic webhook processor"""
    return GenericWebhookProcessor(
        template_repo=template_repo,
        knowledge_store=knowledge_store,
        renderer=renderer
    )
