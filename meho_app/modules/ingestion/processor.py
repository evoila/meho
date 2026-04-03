# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Generic webhook processor.

Processes webhook events using configuration-driven templates.
Zero system-specific logic!
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from meho_app.core.errors import NotFoundError, ValidationError
from meho_app.core.otel import get_logger
from meho_app.modules.ingestion.repository import EventTemplateRepository
from meho_app.modules.ingestion.template_renderer import TemplateRenderer
from meho_app.modules.knowledge.knowledge_store import KnowledgeStore
from meho_app.modules.knowledge.schemas import (
    KnowledgeChunk,
    KnowledgeChunkCreate,
    KnowledgeType,
)

logger = get_logger(__name__)


# Event retention policies (in days)
EVENT_RETENTION_POLICIES = {
    "deployment": 30,  # Keep deployment history longer (useful for rollback)
    "sync": 14,  # ArgoCD sync events
    "sync_status": 14,
    "alert": 7,  # Generic alerts
    "alert_critical": 30,  # Critical alerts kept longer
    "crash": 7,  # Pod crashes
    "pod_event": 7,  # K8s pod events
    "push": 14,  # Git commits
    "commit": 14,
    "network_event": 7,  # Network events
    "default": 7,  # Default for unknown event types
}


class GenericWebhookProcessor:
    """
    Generic processor for webhook events.

    Uses event templates to convert webhook payloads into knowledge chunks.
    Works with ANY system - GitHub, ArgoCD, K8s, Datadog, custom, etc.
    """

    def __init__(
        self,
        template_repo: EventTemplateRepository,
        knowledge_store: KnowledgeStore,
        renderer: TemplateRenderer | None = None,
    ) -> None:
        """
        Initialize processor.

        Args:
            template_repo: Repository for event templates
            knowledge_store: Knowledge store for creating chunks
            renderer: Optional template renderer (creates default if not provided)
        """
        self.template_repo = template_repo
        self.knowledge_store = knowledge_store
        self.renderer = renderer or TemplateRenderer()

    async def process_webhook(
        self,
        connector_id: str,
        event_type: str,
        payload: dict[str, Any],
        tenant_id: str,
        system_id: str | None = None,
    ) -> KnowledgeChunk:
        """
        Process webhook event using configured template.

        Steps:
        1. Get template for connector + event_type
        2. Render text using template
        3. Generate tags using rules
        4. Check if event is an issue
        5. Create knowledge chunk

        Args:
            connector_id: Connector ID (links to template)
            event_type: Event type (e.g., "push", "alert")
            payload: Webhook payload (JSON)
            tenant_id: Tenant ID
            system_id: Optional system ID

        Returns:
            Created knowledge chunk

        Raises:
            NotFoundError: If no template found for connector + event_type
            ValidationError: If template rendering fails
        """
        logger.info(
            f"Processing webhook: connector={connector_id}, event={event_type}, tenant={tenant_id}"
        )

        # 1. Get template
        template = await self.template_repo.get_template(
            connector_id=connector_id, event_type=event_type
        )

        if not template:
            raise NotFoundError(
                f"No template found for {connector_id}/{event_type}. "
                f"Create one using POST /ingestion/templates"
            )

        # Security: Verify template belongs to this tenant
        if template.tenant_id != tenant_id:
            raise ValidationError(f"Template {template.id} does not belong to tenant {tenant_id}")

        try:
            # 2. Render text
            text = self.renderer.render_text(template.text_template, payload)  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access

            # 3. Generate tags
            tags = self.renderer.render_tags(template.tag_rules, payload)  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access

            # 4. Check if this is an issue
            if template.issue_detection_rule:
                is_issue = self.renderer.evaluate_boolean(
                    template.issue_detection_rule,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
                    payload,
                )
                if is_issue:
                    tags.append("issue")

            logger.info(
                f"Rendered template: text_length={len(text)}, "
                f"tags={len(tags)}, issue={'issue' in tags}"
            )

            # 5. Determine event lifecycle
            retention_days = self._get_retention_days(event_type)
            expires_at = datetime.now(tz=UTC) + timedelta(days=retention_days)

            # Events have higher priority if they're issues
            priority = 10 if "issue" in tags else 5

            logger.info(
                f"Event lifecycle: retention={retention_days} days, "
                f"expires_at={expires_at.isoformat()}, priority={priority}"
            )

            # 6. Create knowledge chunk with lifecycle metadata
            chunk = await self.knowledge_store.add_chunk(
                KnowledgeChunkCreate(
                    text=text,
                    tags=tags,
                    tenant_id=tenant_id,
                    connector_id=connector_id,
                    expires_at=expires_at,
                    knowledge_type=KnowledgeType.EVENT,
                    priority=priority,
                )
            )

            logger.info(f"Created knowledge chunk: {chunk.id}")
            return chunk

        except Exception as e:
            logger.error(f"Failed to process webhook: {type(e).__name__}: {e}", exc_info=True)
            raise ValidationError(f"Failed to process webhook: {e!s}") from e

    def _get_retention_days(self, event_type: str) -> int:
        """
        Get retention period for event type.

        Different event types have different retention policies:
        - deployment: 30 days (useful for rollback analysis)
        - alert_critical: 30 days
        - commit/push: 14 days
        - pod_event: 7 days
        - default: 7 days

        Args:
            event_type: Type of event (e.g., "push", "alert", "deployment")

        Returns:
            Retention period in days
        """
        return EVENT_RETENTION_POLICIES.get(event_type, EVENT_RETENTION_POLICIES["default"])
