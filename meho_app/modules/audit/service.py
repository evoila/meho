# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Audit service layer.

Provides methods for logging, querying, and purging audit events.
Designed for synchronous in-transaction logging (~1ms per INSERT).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.core.otel import get_logger
from meho_app.modules.audit.models import AuditEvent

logger = get_logger(__name__)


class AuditService:
    """Service for audit event lifecycle: log, query, purge."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def log_event(
        self,
        *,
        tenant_id: str,
        user_id: str,
        event_type: str,
        action: str,
        resource_type: str,
        resource_id: str | None = None,
        resource_name: str | None = None,
        details: dict[str, Any] | None = None,
        result: str = "success",
        user_email: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> AuditEvent:
        """
        Create an audit event in the current transaction.

        The event is flushed (not committed) so that it shares the
        caller's transaction -- if the caller rolls back, the audit
        event rolls back too.

        Args:
            tenant_id: Tenant identifier.
            user_id: User identifier (email or sub).
            event_type: Dot-separated event type (e.g. 'connector.create').
            action: Verb (create, update, delete, login, logout, approve, deny).
            resource_type: Resource category (connector, knowledge_doc, config, session).
            resource_id: Optional UUID or identifier of affected resource.
            resource_name: Optional human-readable name of the resource.
            details: Optional JSONB payload with changes, metadata, or context.
            result: Outcome -- 'success', 'failure', or 'error'.
            user_email: Optional display email.
            ip_address: Optional client IP (IPv4/IPv6).
            user_agent: Optional client user-agent string.

        Returns:
            The created AuditEvent instance.
        """
        event = AuditEvent(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            user_id=user_id,
            user_email=user_email,
            event_type=event_type,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            resource_name=resource_name,
            details=details,
            result=result,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        self.session.add(event)
        await self.session.flush()
        return event

    # ------------------------------------------------------------------
    # Read -- admin cross-tenant view
    # ------------------------------------------------------------------

    async def query_events(
        self,
        tenant_id: str,
        *,
        event_type: str | None = None,
        resource_type: str | None = None,
        user_id: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[AuditEvent], int]:
        """
        Query audit events for a tenant (admin view).

        Returns:
            Tuple of (events, total_count).
        """
        base = sa.select(AuditEvent).where(AuditEvent.tenant_id == tenant_id)

        if event_type:
            base = base.where(AuditEvent.event_type == event_type)
        if resource_type:
            base = base.where(AuditEvent.resource_type == resource_type)
        if user_id:
            base = base.where(AuditEvent.user_id == user_id)

        # Total count
        count_stmt = sa.select(sa.func.count()).select_from(base.subquery())
        total = (await self.session.execute(count_stmt)).scalar_one()

        # Fetch page
        stmt = base.order_by(AuditEvent.created_at.desc()).offset(offset).limit(limit)
        result = await self.session.execute(stmt)
        events = list(result.scalars().all())

        return events, total

    # ------------------------------------------------------------------
    # Read -- user personal activity
    # ------------------------------------------------------------------

    async def get_user_activity(
        self,
        tenant_id: str,
        user_id: str,
        *,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[AuditEvent], int]:
        """
        Get a user's own activity log.

        Returns:
            Tuple of (events, total_count).
        """
        base = sa.select(AuditEvent).where(
            AuditEvent.tenant_id == tenant_id,
            AuditEvent.user_id == user_id,
        )

        count_stmt = sa.select(sa.func.count()).select_from(base.subquery())
        total = (await self.session.execute(count_stmt)).scalar_one()

        stmt = base.order_by(AuditEvent.created_at.desc()).offset(offset).limit(limit)
        result = await self.session.execute(stmt)
        events = list(result.scalars().all())

        return events, total

    # ------------------------------------------------------------------
    # Maintenance -- purge old events
    # ------------------------------------------------------------------

    async def purge_old_events(self, retention_days: int = 90) -> int:
        """
        Delete audit events older than *retention_days*.

        Args:
            retention_days: Number of days to retain (default 90).

        Returns:
            Number of rows deleted.
        """
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        stmt = (
            sa.delete(AuditEvent)
            .where(AuditEvent.created_at < cutoff)
            .execution_options(synchronize_session=False)
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        deleted: int = result.rowcount or 0  # type: ignore[attr-defined]  # SQLAlchemy Result.rowcount exists at runtime
        if deleted:
            logger.info(f"Purged {deleted} audit events older than {retention_days} days")
        return deleted
