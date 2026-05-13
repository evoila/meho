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
from meho_backplane.broadcast.probe import broadcast_readiness_probe

__all__ = [
    "broadcast_readiness_probe",
    "dispose_broadcast_client",
    "get_broadcast_client",
    "reset_broadcast_client_for_testing",
]
