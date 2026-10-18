# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Notice when the caller of a synchronous request goes away (#3696).

A synchronous ``call_operation`` (REST ``POST /api/v1/operations/call`` or
the MCP ``tools/call`` POST) keeps running when the client gives up and
closes the connection. The ASGI server does not cancel the request, so a
long vendor call (for example a multi-hour OVF deploy) finishes on its own.
That is on purpose: stopping half way could leave a half-applied change at
the vendor.

What was missing is a trace. :func:`watch_caller` listens for the ASGI
``http.disconnect`` message while the request runs. When it arrives, it
stores the time. :func:`caller_gone_at` returns that time, and the
dispatcher's audit writer copies it into ``payload["caller_gone_at"]`` on
every audit row written after the caller left. So the audit row of the
finished operation shows that nobody was waiting for the answer.

The watcher only reads from ``receive`` after the request body was fully
read. At that point the server sends nothing more except
``http.disconnect``. It stops after the first message, so a test double
that returns other messages cannot make it spin.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from starlette.types import Receive

__all__ = ["caller_gone_at", "watch_caller"]

_log = structlog.get_logger(__name__)


@dataclass
class _CallerState:
    gone_at: datetime | None = None


# The watcher task writes into this shared object. A plain value in a
# ContextVar would not work: a task gets a copy of the context, so a
# value it sets is not seen by the request task.
_caller_state_var: ContextVar[_CallerState | None] = ContextVar("caller_state", default=None)


def caller_gone_at() -> datetime | None:
    """Return when the caller disconnected, or ``None`` if it is still there."""
    state = _caller_state_var.get()
    return None if state is None else state.gone_at


async def _wait_for_disconnect(receive: Receive, state: _CallerState) -> None:
    try:
        message = await receive()
    except Exception:
        # A broken receive channel is not our problem to report here.
        _log.debug("caller_watch_receive_failed", exc_info=True)
        return
    if message.get("type") == "http.disconnect":
        state.gone_at = datetime.now(UTC)
        _log.warning("caller_gone_during_request", caller_gone_at=state.gone_at.isoformat())


@asynccontextmanager
async def watch_caller(receive: Receive) -> AsyncIterator[None]:
    """Record the disconnect time of the caller while the block runs.

    Call it only after the request body has been read. The work inside the
    block is never cancelled: a gone caller only leaves a mark for the
    audit row.
    """
    state = _CallerState()
    token = _caller_state_var.set(state)
    task = asyncio.create_task(_wait_for_disconnect(receive, state))
    try:
        yield
    finally:
        task.cancel()
        try:
            await asyncio.wait({task})
        finally:
            _caller_state_var.reset(token)
