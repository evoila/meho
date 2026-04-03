# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Detailed Event Models for Deep Observability.

This module provides re-exports of event types and builders for backward
compatibility. The actual implementations have been split into:

- event_types.py: TokenUsage, EventDetails, DetailedEvent dataclasses
- event_builders.py: estimate_cost(), serialize_pydantic_messages(), MODEL_COSTS

Example:
    >>> from meho_app.modules.agents.base.detailed_events import (
    ...     DetailedEvent,
    ...     EventDetails,
    ...     TokenUsage,
    ...     estimate_cost,
    ... )
    >>> details = EventDetails(
    ...     llm_prompt="You are MEHO...",
    ...     llm_response="I will search for VMs...",
    ...     token_usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
    ... )
    >>> event = DetailedEvent.create(
    ...     event_type="thought",
    ...     summary="Analyzing request for VM inventory",
    ...     details=details,
    ... )
"""

# Re-export types from event_types module
# Re-export utilities from event_builders module
from meho_app.modules.agents.base.event_builders import (
    MODEL_COSTS,
    estimate_cost,
    serialize_pydantic_messages,
)
from meho_app.modules.agents.base.event_types import (
    DetailedEvent,
    EventDetails,
    TokenUsage,
)

__all__ = [
    # Utilities
    "MODEL_COSTS",
    "DetailedEvent",
    "EventDetails",
    # Types
    "TokenUsage",
    "estimate_cost",
    "serialize_pydantic_messages",
]
