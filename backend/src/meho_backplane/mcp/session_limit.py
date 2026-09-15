# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-tenant cap on new MCP sessions per window (#3500).

MEHO holds **no stateful MCP session store** -- a session id is issued
on ``initialize`` purely for audit correlation and then forgotten
(``mcp/server.py`` ``_issue_mcp_session_id``). A precise "concurrent
sessions" count is therefore unknowable without building a session
registry, which the finding (meho-internal#321) explicitly did not ask
for. The cheap, deterministic backstop that MEHO *can* enforce is a cap
on how many new session handshakes a tenant may start per window: every
``initialize` is one new session, so bounding ``initialize`` volume per
tenant bounds session creation -- the abuse vector at the stand (a
visitor tab-spamming reconnects, a client relaunch loop).

Same fixed-window ``INCR`` + ``EXPIRE`` shape as the dispatch and
announce limiters, keyed per tenant. A cap of ``0`` (the default)
disables it with no Valkey round-trip, so existing tenants are
unaffected until an operator opts in.
"""

from __future__ import annotations

import time
from typing import Final
from uuid import UUID

from prometheus_client import Counter

from meho_backplane.broadcast.client import get_broadcast_client
from meho_backplane.operations.dispatch_limits import resolve_int_override
from meho_backplane.settings import Settings

__all__ = [
    "MCP_SESSION_LIMITED_TOTAL",
    "MCP_SESSION_LIMIT_WINDOW_SECONDS",
    "check_mcp_session_cap",
    "resolve_mcp_session_cap",
]

#: Fixed window width, matching the ``*_per_minute`` cap unit.
MCP_SESSION_LIMIT_WINDOW_SECONDS: Final[int] = 60

#: MCP ``initialize`` handshakes rejected by the per-tenant session cap.
MCP_SESSION_LIMITED_TOTAL: Counter = Counter(
    "mcp_session_limited_total",
    "MCP initialize handshakes rejected by the per-tenant session cap (#3500).",
)


def resolve_mcp_session_cap(settings: Settings, tenant_id: UUID) -> int:
    """New-sessions-per-window cap for *tenant_id* (0 = disabled)."""
    return resolve_int_override(
        settings.mcp_session_start_limit_per_minute_overrides,
        tenant_id,
        settings.mcp_session_start_limit_per_minute,
    )


def _session_window_key(tenant_id: UUID, bucket: int) -> str:
    """Per-``(tenant, window)`` session-start counter key."""
    return f"meho:ratelimit:mcpsession:{tenant_id}:{bucket}"


async def check_mcp_session_cap(tenant_id: UUID, cap: int) -> int | None:
    """Increment the window counter; return a retry-after when over *cap*.

    A *cap* of ``0`` (or negative) disables the check with no Valkey
    round-trip. Otherwise increments the current window's per-tenant
    session-start counter; when the count exceeds *cap* returns the
    whole seconds until the window rolls over, else ``None``.

    Cross-tenant isolation is structural (the key is derived from the
    JWT-bound ``tenant_id``).

    Raises:
        Exception: Any redis-py / Valkey failure propagates verbatim
            (fail-loud, consistent with the dispatch + announce limiters).
    """
    if cap <= 0:
        return None

    window = MCP_SESSION_LIMIT_WINDOW_SECONDS
    now = int(time.time())
    bucket = now // window
    key = _session_window_key(tenant_id, bucket)

    client = get_broadcast_client()
    async with client.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, window)
        count, _ = await pipe.execute()

    if count > cap:
        MCP_SESSION_LIMITED_TOTAL.inc()
        return window - (now % window)
    return None
