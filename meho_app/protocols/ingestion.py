# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Protocol definitions for the Ingestion module.

These protocols define the interfaces for webhook processing
and event template management.
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class IEventTemplateRepository(Protocol):
    """
    Protocol for event template CRUD operations.

    Event templates define how webhook payloads are transformed
    into knowledge chunks.

    Implementations:
        - EventTemplateRepository (PostgreSQL)
    """

    async def create_template(self, template_create: Any) -> Any:
        """Create a new event template."""
        ...

    async def get_template(self, connector_id: str, event_type: str) -> Any | None:
        """Get template by connector ID and event type."""
        ...

    async def get_template_by_id(self, template_id: str) -> Any | None:
        """Get template by ID."""
        ...

    async def list_templates(self, filter: Any) -> list[Any]:
        """List templates with filtering."""
        ...

    async def update_template(self, template_id: str, update: Any) -> Any | None:
        """Update a template."""
        ...

    async def delete_template(self, template_id: str) -> bool:
        """Delete a template. Returns True if deleted."""
        ...


@runtime_checkable
class IWebhookProcessor(Protocol):
    """
    Protocol for processing incoming webhooks.

    Webhook processors transform external events into knowledge chunks
    and other internal representations.

    Implementations:
        - GenericWebhookProcessor
    """

    async def process(
        self,
        path: str,
        payload: dict[str, Any],
        tenant_id: str,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Process an incoming webhook.

        Args:
            path: Webhook path (used to determine event type)
            payload: Webhook payload
            tenant_id: Tenant ID for the webhook
            headers: Optional HTTP headers

        Returns:
            Processing result with status and any created resources
        """
        ...

    async def validate_webhook(
        self, path: str, payload: dict[str, Any], signature: str | None = None
    ) -> bool:
        """
        Validate webhook authenticity.

        Args:
            path: Webhook path
            payload: Webhook payload
            signature: Optional signature for verification

        Returns:
            True if webhook is valid
        """
        ...
