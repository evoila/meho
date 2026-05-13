# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Broadcast (Valkey Streams) substrate.

G6.1-T1 (#307) lands the async client + lifespan + readiness probe.
T2-T6 (#308-#312) layer event schema, publish-on-write, SSE endpoint,
MCP resource, and the ``meho status --watch`` CLI on top of this
foundation.
"""

from meho_backplane.broadcast.client import (
    dispose_broadcast_client,
    get_broadcast_client,
    reset_broadcast_client_for_testing,
)
from meho_backplane.broadcast.events import (
    BroadcastEvent,
    classify_op,
    redact_payload,
)
from meho_backplane.broadcast.probe import broadcast_readiness_probe
from meho_backplane.broadcast.publisher import (
    BROADCAST_EVENTS_PUBLISHED_TOTAL,
    BROADCAST_MAXLEN,
    BROADCAST_PUBLISH_ERRORS_TOTAL,
    publish_event,
)

__all__ = [
    "BROADCAST_EVENTS_PUBLISHED_TOTAL",
    "BROADCAST_MAXLEN",
    "BROADCAST_PUBLISH_ERRORS_TOTAL",
    "BroadcastEvent",
    "broadcast_readiness_probe",
    "classify_op",
    "dispose_broadcast_client",
    "get_broadcast_client",
    "publish_event",
    "redact_payload",
    "reset_broadcast_client_for_testing",
]
