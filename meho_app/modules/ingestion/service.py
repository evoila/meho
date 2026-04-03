# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Ingestion module public service interface.
"""

# Import protocols for type hints (import directly to avoid circular imports)
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from .processor import GenericWebhookProcessor
from .repository import EventTemplateRepository

if TYPE_CHECKING:
    from meho_app.protocols.ingestion import IEventTemplateRepository, IWebhookProcessor


class IngestionService:
    """
    Public API for the ingestion module.

    Handles webhook processing and background ingestion jobs.

    Supports two construction patterns:

    1. Session-based (backward compatible):
        service = IngestionService(session)

    2. Protocol-based (for dependency injection):
        service = IngestionService.from_protocols(
            template_repo=mock_template_repo,
            processor=mock_processor,
        )
    """

    def __init__(
        self,
        session: AsyncSession | None = None,
        *,
        template_repo: Optional["IEventTemplateRepository"] = None,
        processor: Optional["IWebhookProcessor"] = None,
    ) -> None:
        """
        Initialize IngestionService.

        Args:
            session: AsyncSession (creates concrete implementations)
            template_repo: Optional event template repository
            processor: Optional webhook processor
        """
        self.session = session
        if session is not None:
            self.template_repo = template_repo or EventTemplateRepository(session)
            if processor is None:
                from meho_app.modules.knowledge.embeddings import get_embedding_provider
                from meho_app.modules.knowledge.knowledge_store import KnowledgeStore
                from meho_app.modules.knowledge.repository import KnowledgeRepository

                knowledge_repo = KnowledgeRepository(session)
                embedding_provider = get_embedding_provider()
                knowledge_store = KnowledgeStore(
                    repository=knowledge_repo,
                    embedding_provider=embedding_provider,
                )
                self.processor: IWebhookProcessor | GenericWebhookProcessor | None = (
                    GenericWebhookProcessor(self.template_repo, knowledge_store)  # type: ignore[arg-type]  # IEventTemplateRepository is a protocol implemented by EventTemplateRepository
                )
            else:
                self.processor = processor
        elif template_repo is not None:
            self.template_repo = template_repo
            self.processor = processor
        else:
            raise ValueError(
                "IngestionService requires either 'session' or 'template_repo' argument"
            )

    @classmethod
    def from_protocols(
        cls,
        template_repo: "IEventTemplateRepository",
        processor: Optional["IWebhookProcessor"] = None,
    ) -> "IngestionService":
        """
        Create IngestionService from protocol implementations.

        Args:
            template_repo: Event template repository implementation
            processor: Optional webhook processor

        Returns:
            Configured IngestionService instance
        """
        return cls(
            session=None,
            template_repo=template_repo,
            processor=processor,
        )

    async def process_webhook(
        self,
        path: str,
        payload: dict[str, Any],
        tenant_id: str,
    ) -> dict[str, Any]:
        """Process an incoming webhook."""
        # Process using processor
        result = await self.processor.process(path, payload, tenant_id)  # type: ignore[union-attr]

        return {
            "status": result.get("status", "completed"),
            "message": result.get("message", "Webhook processed"),
        }

    async def get_template(self, template_id: str) -> Any:
        """Get an event template by ID."""
        return await self.template_repo.get_by_id(template_id)  # type: ignore[union-attr]

    async def list_templates(
        self,
        tenant_id: str,
        limit: int = 50,
    ) -> Any:
        """List event templates."""
        return await self.template_repo.list(tenant_id=tenant_id, limit=limit)  # type: ignore[union-attr]


def get_ingestion_service(session: AsyncSession) -> IngestionService:
    """Factory function for getting an IngestionService instance."""
    return IngestionService(session)
