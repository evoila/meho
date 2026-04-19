# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""SSE (Server-Sent Events) utilities for MEHO agents.

Exports:
    EventEmitter: Utility for emitting SSE events from agents/tools/nodes
    EventFormatter: Utility for formatting events for transcript persistence
    EventRegistry: Registry for documenting event types
    EventSchema: Schema definition for event types
"""

from __future__ import annotations

from meho_app.modules.agents.sse.broadcaster import RedisSSEBroadcaster
from meho_app.modules.agents.sse.emitter import EventEmitter
from meho_app.modules.agents.sse.event_formatter import EventFormatter
from meho_app.modules.agents.sse.registry import EventRegistry, EventSchema

__all__ = [
    "EventEmitter",
    "EventFormatter",
    "EventRegistry",
    "EventSchema",
    "RedisSSEBroadcaster",
]
