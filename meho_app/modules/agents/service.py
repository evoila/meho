# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Agent module public service interface.

Handles chat sessions and agent execution.
Note: Workflow/Plan methods removed - ReAct agent operates without persistent storage.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, case, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    ApprovalRequestModel,
    ApprovalStatus,
    ChatMessageModel,
    ChatSessionModel,
    validate_visibility_upgrade,
)


class AgentService:
    """
    Public API for the agent module.

    Handles chat sessions and agent execution.
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        Initialize AgentService.

        Args:
            session: AsyncSession for database operations
        """
        self.session = session

    # Chat session operations
    async def create_chat_session(
        self,
        tenant_id: str,
        user_id: str,
        title: str | None = None,
        session_id: str | None = None,
        visibility: str = "private",
        created_by_name: str | None = None,
        trigger_source: str | None = None,
    ) -> ChatSessionModel:
        """Create a new chat session.

        Args:
            tenant_id: Tenant the session belongs to.
            user_id: User creating the session.
            title: Optional session title (auto-generated if not provided).
            session_id: Optional explicit session ID.
            visibility: Session visibility level (private/group/tenant). Defaults to private.
            created_by_name: Display name for the creator (e.g., "Alice", "Alertmanager").
            trigger_source: Source that triggered creation (null=human, "event", "scheduled_task").

        Returns:
            The created ChatSessionModel.
        """
        session = ChatSessionModel(
            id=UUID(session_id) if session_id else uuid4(),
            tenant_id=tenant_id,
            user_id=user_id,
            title=title or f"Chat {datetime.now(tz=UTC).strftime('%Y-%m-%d %H:%M')}",
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
            visibility=visibility,
            created_by_name=created_by_name,
            trigger_source=trigger_source,
        )
        self.session.add(session)
        await self.session.commit()  # Commit to persist the session
        await self.session.refresh(session)
        return session

    async def get_chat_session(
        self,
        session_id: str,
        include_messages: bool = True,
    ) -> ChatSessionModel | None:
        """Get a chat session by ID."""
        try:
            session_uuid = UUID(session_id)
        except ValueError:
            return None

        stmt = select(ChatSessionModel).where(ChatSessionModel.id == session_uuid)
        result = await self.session.execute(stmt)
        chat_session = result.scalar_one_or_none()

        if chat_session and include_messages:
            # Eagerly load messages
            await self.session.refresh(chat_session, ["messages"])

        return chat_session

    async def list_chat_sessions(
        self,
        tenant_id: str,
        user_id: str,
        limit: int = 50,
    ) -> list[ChatSessionModel]:
        """List chat sessions for a user."""
        stmt = (
            select(ChatSessionModel)
            .where(ChatSessionModel.tenant_id == tenant_id)
            .where(ChatSessionModel.user_id == user_id)
            .order_by(desc(ChatSessionModel.updated_at))
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def add_chat_message(
        self,
        session_id: str,
        role: str,
        content: str,
        message_data: dict[str, Any] | None = None,
        sender_id: str | None = None,
        sender_name: str | None = None,
    ) -> ChatMessageModel:
        """Add a message to a chat session.

        Args:
            session_id: Chat session UUID string.
            role: Message role ('user' or 'assistant').
            content: Text content of the message.
            message_data: Optional full PydanticAI message structure.
            sender_id: Optional sender user ID (Phase 39: war room attribution).
            sender_name: Optional sender display name (Phase 39: war room attribution).

        Returns:
            The created ChatMessageModel.
        """
        try:
            session_uuid = UUID(session_id)
        except ValueError:
            raise ValueError(f"Invalid session_id: {session_id}") from None

        message = ChatMessageModel(
            id=uuid4(),
            session_id=session_uuid,
            role=role,
            content=content,
            message_data=message_data,
            sender_id=sender_id,
            sender_name=sender_name,
            created_at=datetime.now(tz=UTC),
        )
        self.session.add(message)

        # Update session's updated_at timestamp
        stmt = select(ChatSessionModel).where(ChatSessionModel.id == session_uuid)
        result = await self.session.execute(stmt)
        chat_session = result.scalar_one_or_none()
        if chat_session:
            chat_session.updated_at = datetime.now(tz=UTC)  # type: ignore[assignment]  # SQLAlchemy ORM attribute assignment

        await self.session.commit()  # Commit to persist the message
        await self.session.refresh(message)

        return message

    async def update_chat_session(
        self,
        session_id: str,
        title: str,
    ) -> ChatSessionModel:
        """Update a chat session's title."""
        try:
            session_uuid = UUID(session_id)
        except ValueError:
            raise ValueError(f"Invalid session_id: {session_id}") from None

        stmt = select(ChatSessionModel).where(ChatSessionModel.id == session_uuid)
        result = await self.session.execute(stmt)
        chat_session = result.scalar_one_or_none()

        if not chat_session:
            raise ValueError(f"Session {session_id} not found")

        chat_session.title = title  # type: ignore[assignment]  # SQLAlchemy ORM attribute assignment
        chat_session.updated_at = datetime.now(tz=UTC)  # type: ignore[assignment]  # SQLAlchemy ORM attribute assignment
        await self.session.commit()  # Commit to persist the update
        await self.session.refresh(chat_session)

        return chat_session

    async def delete_chat_session(self, session_id: str) -> bool:
        """Delete a chat session."""
        try:
            session_uuid = UUID(session_id)
        except ValueError:
            return False

        stmt = select(ChatSessionModel).where(ChatSessionModel.id == session_uuid)
        result = await self.session.execute(stmt)
        chat_session = result.scalar_one_or_none()

        if not chat_session:
            return False

        await self.session.delete(chat_session)
        await self.session.commit()  # Commit to persist the deletion
        return True

    # =========================================================================
    # Group Session Operations (Phase 38)
    # =========================================================================

    async def list_team_sessions(
        self,
        tenant_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List non-private sessions visible to all users in a tenant.

        Queries ChatSessionModel for group/tenant sessions, LEFT JOINs on
        ApprovalRequestModel to count PENDING approvals per session.

        Args:
            tenant_id: Tenant to filter sessions for.
            limit: Maximum number of sessions to return (default 50).

        Returns:
            List of dicts with session info including derived status and
            pending_approval_count.
        """
        now = datetime.now(tz=UTC)
        stmt = (
            select(
                ChatSessionModel,
                func.count(
                    case(
                        (
                            and_(
                                ApprovalRequestModel.status == ApprovalStatus.PENDING,
                                or_(
                                    ApprovalRequestModel.expires_at.is_(None),
                                    ApprovalRequestModel.expires_at >= now,
                                ),
                            ),
                            1,
                        ),
                    )
                ).label("pending_count"),
            )
            .outerjoin(
                ApprovalRequestModel,
                ChatSessionModel.id == ApprovalRequestModel.session_id,
            )
            .where(ChatSessionModel.tenant_id == tenant_id)
            .where(ChatSessionModel.visibility != "private")
            .group_by(ChatSessionModel.id)
            .order_by(desc(ChatSessionModel.updated_at))
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        rows = result.all()

        return [
            {
                "id": str(s.id),
                "title": s.title,
                "visibility": s.visibility,
                "created_by_name": s.created_by_name,
                "trigger_source": s.trigger_source,
                "status": "awaiting_approval" if pc > 0 else "idle",
                "pending_approval_count": pc,
                "created_at": s.created_at,
                "updated_at": s.updated_at,
            }
            for s, pc in rows
        ]

    async def update_session_visibility(
        self,
        session_id: str,
        visibility: str,
        _user_id: str,
    ) -> ChatSessionModel:
        """Upgrade a session's visibility level.

        Enforces upgrade-only: private -> group -> tenant. Downgrades
        and same-level transitions are rejected with ValueError.

        Args:
            session_id: Session to update.
            visibility: New visibility level.
            user_id: User requesting the change (for audit purposes).

        Returns:
            The updated ChatSessionModel.

        Raises:
            ValueError: If session not found or visibility downgrade attempted.
        """
        try:
            session_uuid = UUID(session_id)
        except ValueError:
            raise ValueError(f"Invalid session_id: {session_id}") from None

        stmt = select(ChatSessionModel).where(ChatSessionModel.id == session_uuid)
        result = await self.session.execute(stmt)
        chat_session = result.scalar_one_or_none()

        if not chat_session:
            raise ValueError(f"Session {session_id} not found")

        if not validate_visibility_upgrade(chat_session.visibility, visibility):  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
            raise ValueError(
                f"Cannot change visibility from '{chat_session.visibility}' to '{visibility}'. "
                "Visibility can only be upgraded (private -> group -> tenant)."
            )

        chat_session.visibility = visibility  # type: ignore[assignment]  # SQLAlchemy ORM attribute assignment
        chat_session.updated_at = datetime.now(tz=UTC)  # type: ignore[assignment]  # SQLAlchemy ORM attribute assignment
        await self.session.commit()
        await self.session.refresh(chat_session)
        return chat_session


def get_agent_service(session: AsyncSession) -> AgentService:
    """Factory function for getting an AgentService instance."""
    return AgentService(session)
