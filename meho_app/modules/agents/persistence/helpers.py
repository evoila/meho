# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Shared helper functions for transcript persistence.

This module provides reusable functions for creating and managing
transcript collectors across different agent implementations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from meho_app.core.otel import get_logger

if TYPE_CHECKING:
    from meho_app.modules.agents.persistence.transcript_collector import (
        TranscriptCollector,
    )

logger = get_logger(__name__)


async def create_transcript_collector(
    dependencies: Any,
    session_id: str,
    user_message: str,
    agent_name: str,
) -> TranscriptCollector | None:
    """Create a transcript collector for deep observability.

    This function is used by both orchestrator and specialist_agent
    to enable unified observability.

    Args:
        dependencies: The dependencies container with db_session attribute.
                     For old agent: MEHODependencies
                     For new agent: MEHODependencies or similar
        session_id: The chat session ID (string, will be converted to UUID).
        user_message: The user's input message/query.
        agent_name: The name of the agent (e.g., "orchestrator", "react", "k8", "generic").

    Returns:
        TranscriptCollector if a database session is available, None otherwise.
        Returns None gracefully if:
        - dependencies doesn't have db_session attribute
        - db_session is None
        - Any exception occurs during creation
    """
    try:
        # Check if we have a database session available
        if not hasattr(dependencies, "db_session"):
            logger.debug("No db_session available, skipping transcript collection")
            return None

        db_session = dependencies.db_session
        if db_session is None:
            return None

        from meho_app.modules.agents.persistence.transcript_collector import (
            TranscriptCollector,
        )
        from meho_app.modules.agents.persistence.transcript_service import (
            TranscriptService,
        )

        # Create service and transcript
        service = TranscriptService(db_session)
        transcript = await service.create_transcript(
            session_id=UUID(session_id),
            user_query=user_message,
            agent_type=agent_name,
        )

        # Create and return collector
        collector = TranscriptCollector(
            transcript_id=transcript.id,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
            session_id=UUID(session_id),
            service=service,
        )

        logger.debug(f"Created transcript {transcript.id} for session {session_id}")
        return collector

    except Exception as e:
        logger.warning(f"Failed to create transcript collector: {e}")
        return None
