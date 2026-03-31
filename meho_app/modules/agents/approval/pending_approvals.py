# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""In-process approval event registry for pause/resume.

Maps session_id -> PendingApproval with asyncio.Event for signaling.
Single-process only (current deployment). For multi-worker, replace
asyncio.Event with Redis pub/sub.

Phase 5: Replaces the old re-send-message pattern with in-process
pause/resume that preserves the agent's investigation context
(scratchpad, step count, pending tool/args).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PendingApproval:
    """Tracks a pending approval with its asyncio.Event.

    Attributes:
        event: asyncio.Event signaled when operator approves/denies.
        approved: Whether the operator approved (set before event.set()).
        approval_id: DB approval request UUID (for audit trail linkage).
        tool_name: Tool that requires approval.
        tool_args: Arguments for the tool.
    """

    event: asyncio.Event
    approved: bool = False
    approval_id: str | None = None
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)


# Global registry. Cleaned up in finally blocks.
PENDING_APPROVALS: dict[str, PendingApproval] = {}


def register_pending(
    session_id: str,
    tool_name: str,
    tool_args: dict[str, Any],
    approval_id: str | None = None,
) -> PendingApproval:
    """Create and register a pending approval event.

    Args:
        session_id: Session identifier (key for lookup).
        tool_name: Tool that requires approval.
        tool_args: Arguments for the tool.
        approval_id: Optional DB approval request UUID.

    Returns:
        PendingApproval with an unset asyncio.Event.
    """
    pending = PendingApproval(
        event=asyncio.Event(),
        tool_name=tool_name,
        tool_args=tool_args,
        approval_id=approval_id,
    )
    PENDING_APPROVALS[session_id] = pending
    return pending


def resolve_pending(session_id: str, approved: bool) -> bool:
    """Signal the pending approval event.

    CRITICAL: Sets approved state BEFORE event.set() to avoid a race
    where the waiting coroutine reads stale state (Research Pitfall 2).

    Args:
        session_id: Session identifier.
        approved: Whether the operator approved the operation.

    Returns:
        True if a pending approval was found and signaled, False otherwise.
    """
    pending = PENDING_APPROVALS.get(session_id)
    if not pending:
        return False
    # CRITICAL: Set state BEFORE event.set() (Research Pitfall 2)
    pending.approved = approved
    pending.event.set()
    return True


def cleanup_pending(session_id: str) -> None:
    """Remove pending approval entry (call in finally blocks).

    Prevents memory leaks when SSE streams end or errors occur.

    Args:
        session_id: Session identifier to clean up.
    """
    PENDING_APPROVALS.pop(session_id, None)
