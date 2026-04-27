# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Context variable for transcript collector propagation.

This module provides a way to propagate the TranscriptCollector through
the async call chain without threading it through every function signature.

The context variable is task-local, meaning each async task has its own
isolated value. This is safe for concurrent request handling.

Example:
    >>> from meho_app.modules.agents.persistence.event_context import (
    ...     set_transcript_collector,
    ...     get_transcript_collector,
    ... )
    >>>
    >>> # In agent setup
    >>> set_transcript_collector(collector)
    >>>
    >>> # Deep in HTTP client (no need to pass collector through params)
    >>> collector = get_transcript_collector()
    >>> if collector:
    ...     event = collector.create_operation_event(...)
    ...     await collector.add(event)
    >>>
    >>> # In finally block
    >>> set_transcript_collector(None)
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from meho_app.modules.agents.persistence.transcript_collector import (
        TranscriptCollector,
    )

# Context variable holding the current transcript collector
# Default is None (no collector in context)
_current_collector: ContextVar[TranscriptCollector | None] = ContextVar(
    "transcript_collector", default=None
)


def set_transcript_collector(collector: TranscriptCollector | None) -> None:
    """Set the transcript collector for the current async context.

    Call this when starting agent execution to enable event logging
    from any code path (including HTTP client, tool execution, etc.).

    Args:
        collector: The TranscriptCollector instance, or None to clear.
    """
    _current_collector.set(collector)


def get_transcript_collector() -> TranscriptCollector | None:
    """Get the transcript collector from the current async context.

    Returns:
        The TranscriptCollector if set, None otherwise.
        Returns None gracefully - callers should check before using.
    """
    return _current_collector.get()


def has_transcript_collector() -> bool:
    """Check if a transcript collector is available in the current context.

    Returns:
        True if a collector is set, False otherwise.
    """
    return _current_collector.get() is not None
