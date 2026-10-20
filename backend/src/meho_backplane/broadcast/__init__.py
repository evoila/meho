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
    IDEMPOTENCY_KEY_MAX_CHARS,
    MAX_TARGETS,
    PLANNED_OP_CLASS_VALUES,
    TARGET_MAX_CHARS,
    TTL_DEFAULT_MINUTES,
    TTL_MAX_MINUTES,
    TTL_MIN_MINUTES,
    WORK_REF_MAX_CHARS,
    AgentAnnouncementEvent,
    PlannedOpClass,
)
from meho_backplane.broadcast.announcement_claims import (
    AnnouncementClaimError,
    build_trusted_announcement,
)
from meho_backplane.broadcast.archive_cursor import (
    InvalidArchiveCursorError,
    encode_watch_checkpoint,
)
from meho_backplane.broadcast.archive_reader import (
    AnnouncementFilter,
    read_announcement_history,
    watch_announcements,
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
    BROADCAST_MAXLEN,
    BROADCAST_PUBLISH_ERRORS_TOTAL,
    AnnouncementPublishResult,
    publish_agent_announcement,
)
from meho_backplane.broadcast.rate_limit import (
    ANNOUNCE_RATE_LIMIT_WINDOW_SECONDS,
    BROADCAST_ANNOUNCE_RATE_LIMITED_TOTAL,
    AnnounceRateLimitError,
    enforce_announce_rate_limit,
)

__all__ = [
    "ACTIVITY_MAX_CHARS",
    "ANNOUNCE_RATE_LIMIT_WINDOW_SECONDS",
    "BROADCAST_AGENT_ANNOUNCEMENTS_TOTAL",
    "BROADCAST_ANNOUNCE_RATE_LIMITED_TOTAL",
    "BROADCAST_BLOCKING_SOCKET_TIMEOUT_SECONDS",
    "BROADCAST_MAXLEN",
    "BROADCAST_PUBLISH_ERRORS_TOTAL",
    "DEFAULT_WINDOW_MINUTES",
    "IDEMPOTENCY_KEY_MAX_CHARS",
    "MAX_TARGETS",
    "OP_CLASS_ENUM",
    "PLANNED_OP_CLASS_VALUES",
    "TARGET_MAX_CHARS",
    "TTL_DEFAULT_MINUTES",
    "TTL_MAX_MINUTES",
    "TTL_MIN_MINUTES",
    "WORK_REF_MAX_CHARS",
    "AgentAnnouncementEvent",
    "AnnounceRateLimitError",
    "AnnouncementClaimError",
    "AnnouncementFilter",
    "AnnouncementPublishResult",
    "BroadcastEvent",
    "InvalidArchiveCursorError",
    "PlannedOpClass",
    "broadcast_readiness_probe",
    "build_trusted_announcement",
    "classify_op",
    "compute_effective_broadcast_detail",
    "dispose_broadcast_blocking_client",
    "dispose_broadcast_client",
    "encode_watch_checkpoint",
    "enforce_announce_rate_limit",
    "get_broadcast_blocking_client",
    "get_broadcast_client",
    "invalidate_tenant_cache",
    "publish_agent_announcement",
    "read_announcement_history",
    "read_request_override",
    "redact_payload",
    "reset_broadcast_blocking_client_for_testing",
    "reset_broadcast_client_for_testing",
    "reset_overrides_cache_for_testing",
    "watch_announcements",
]
