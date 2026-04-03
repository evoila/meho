# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Utility functions for the Orchestrator Agent.

This module provides shared helper functions used across the orchestrator's
various components.
"""

from __future__ import annotations

import json
from typing import Any

from meho_app.core.otel import get_logger

logger = get_logger(__name__)


def log_structured(
    log_level: int,
    event: str,
    message: str,
    **context: Any,
) -> None:
    """Emit a structured log message with JSON-formatted context.

    This helper produces logs suitable for aggregation by log management systems.
    The context is serialized as JSON and appended to the message.

    Args:
        log_level: Python logging level (e.g., logging.INFO).
        event: Event name for categorization (e.g., "orchestrator_start").
        message: Human-readable message.
        **context: Additional context fields to include in JSON.

    Example:
        log_structured(
            logging.INFO,
            "orchestrator_start",
            "Starting orchestrator",
            session_id="abc123",
            goal_length=150,
        )
        # Output: Starting orchestrator | {"event": "orchestrator_start", "session_id": "abc123", "goal_length": 150}
    """
    context_data = {"event": event, **context}
    # Serialize context, handling non-serializable values
    try:
        context_json = json.dumps(context_data, default=str)
    except (TypeError, ValueError):
        context_json = str(context_data)
    logger.log(log_level, f"{message} | {context_json}")
