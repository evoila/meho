# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Broadcast (Valkey Streams) substrate.

G6.1-T1 (#307) lands the async client + lifespan + readiness probe.
T2-T6 (#308-#312) layer event schema, publish-on-write, SSE endpoint,
MCP resource, and the ``meho status --watch`` CLI on top of this
foundation.
"""

from meho_backplane.broadcast.agent_events import (
    ACTIVITY_MAX_CHARS,
    AgentAnnouncementEvent,
)
from meho_backplane.broadcast.client import (
    BROADCAST_BLOCKING_SOCKET_TIMEOUT_SECONDS,
    dispose_broadcast_blocking_client,
    dispose_broadcast_client,
    get_broadcast_blocking_client,
    get_broadcast_client,
    reset_broadcast_blocking_client_for_testing,
    reset_broadcast_client_for_testing,
)
from meho_backplane.broadcast.events import (
    BroadcastEvent,
    classify_op,
    redact_payload,
)
from meho_backplane.broadcast.history import (
    DEFAULT_WINDOW_MINUTES,
    OP_CLASS_ENUM,
    InvalidSinceError,
    list_recent_events_fail_soft,
    list_recent_events_strict,
)
from meho_backplane.broadcast.overrides import (
    compute_effective_broadcast_detail,
    invalidate_tenant_cache,
    read_request_override,
    reset_overrides_cache_for_testing,
)
from meho_backplane.broadcast.probe import broadcast_readiness_probe
from meho_backplane.broadcast.publisher import (
    BROADCAST_AGENT_ANNOUNCEMENTS_TOTAL,
    BROADCAST_EVENTS_PUBLISHED_TOTAL,
    BROADCAST_MAXLEN,
    BROADCAST_PUBLISH_ERRORS_TOTAL,
    publish_agent_announcement,
    publish_event,
)

__all__ = [
    "ACTIVITY_MAX_CHARS",
    "BROADCAST_AGENT_ANNOUNCEMENTS_TOTAL",
    "BROADCAST_BLOCKING_SOCKET_TIMEOUT_SECONDS",
    "BROADCAST_EVENTS_PUBLISHED_TOTAL",
    "BROADCAST_MAXLEN",
    "BROADCAST_PUBLISH_ERRORS_TOTAL",
    "DEFAULT_WINDOW_MINUTES",
    "OP_CLASS_ENUM",
    "AgentAnnouncementEvent",
    "BroadcastEvent",
    "InvalidSinceError",
    "broadcast_readiness_probe",
    "classify_op",
    "compute_effective_broadcast_detail",
    "dispose_broadcast_blocking_client",
    "dispose_broadcast_client",
    "get_broadcast_blocking_client",
    "get_broadcast_client",
    "invalidate_tenant_cache",
    "list_recent_events_fail_soft",
    "list_recent_events_strict",
    "publish_agent_announcement",
    "publish_event",
    "read_request_override",
    "redact_payload",
    "reset_broadcast_blocking_client_for_testing",
    "reset_broadcast_client_for_testing",
    "reset_overrides_cache_for_testing",
]
