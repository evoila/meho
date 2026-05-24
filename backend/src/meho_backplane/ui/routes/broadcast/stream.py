# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /ui/broadcast/stream`` -- the session-gated SSE bridge.

Initiative #338 (G10.1 Activity broadcast UI), Task #867 (G10.1-T1)
work item #1. This route is the browser-reachable SSE source the
broadcast live-feed view (:mod:`~meho_backplane.ui.routes.broadcast.feed`)
subscribes to via the HTMX ``sse`` extension.

Why a UI-owned stream instead of subscribing to ``/api/v1/feed`` directly
====================================================================

The canonical per-tenant SSE feed is ``GET /api/v1/feed`` (G6.1-T4,
#310). It authenticates via the ``Authorization: Bearer <jwt>`` header
(:func:`meho_backplane.auth.jwt.verify_jwt`). The browser's
``EventSource`` -- which the HTMX ``sse`` extension uses under the hood
-- **cannot set custom request headers** (the WHATWG ``EventSource``
constructor accepts only ``withCredentials``; there is no headers
option). It sends cookies, not a Bearer token. So a logged-in operator's
browser pointing ``sse-connect`` at ``/api/v1/feed`` would be answered
with a 401 and the SSE state machine would tighten into a reconnect
loop.

The chassis dashboard's recent-activity snippet (#866) wired
``sse-connect="/api/v1/feed"`` directly; that wiring is inert for the
same reason (and the snippet only renders a "Connecting..." placeholder
today). G10.1's live feed is the first surface that must *actually*
stream, so it routes through this UI-owned bridge instead.

This route lives under ``/ui/`` so the existing
:class:`~meho_backplane.ui.auth.middleware.UISessionMiddleware` gates it
with the BFF session cookie -- the same auth boundary that gates every
other ``/ui/*`` page. The operator's tenant is taken from the validated
session (:class:`UISessionContext`), never from a query parameter, so
the cross-tenant isolation guarantee is identical to ``/api/v1/feed``:
the stream key is ``meho:feed:{session.tenant_id}`` and there is no
parameter that could redirect it to another tenant's stream.

Frame format reuse
==================

The SSE frame shape (``event: broadcast`` / ``data: <json>`` /
``id: <valkey-id>``), the per-entry filter + parse + skip logic, the
cursor-resolution precedence (``Last-Event-Id`` > ``since`` > ``$``),
and the cursor validation are all reused verbatim from
:mod:`meho_backplane.api.v1.feed` so the two surfaces stay
byte-compatible -- a reconnect that started on one and lands on the
other replays identically. Only the BLOCK/heartbeat loop is restated
here, scoped to the session's tenant key. Importing the feed module's
helpers (rather than copying them) keeps the wire contract
single-sourced; the same module's unit suite already guards them.

Replay
======

``Last-Event-Id`` (sent automatically by ``EventSource`` on reconnect)
and the ``since`` query parameter feed the same cursor resolver the API
route uses, so missed-event replay after a forced drop works the same
way: the Valkey stream is the session state, and a reconnect with the
last seen entry id resumes from that point.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import structlog
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from meho_backplane.api.v1.feed import (
    _HEARTBEAT_INTERVAL_SECONDS,
    _XREAD_BLOCK_MS,
    _XREAD_COUNT,
    _process_entries,
    _resolve_cursor,
    _validate_cursor_or_400,
)
from meho_backplane.broadcast import get_broadcast_client
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session

__all__ = ["build_stream_router"]

_log = structlog.get_logger(__name__)

#: Module-level :class:`fastapi.Depends` closure -- ruff B008 guard
#: (a function call in a default argument position is disallowed except
#: for the FastAPI-blessed call sites in ``extend-immutable-calls``).
_require_ui_session_dep = Depends(require_ui_session)


def _stream_key(tenant_id: object) -> str:
    """Build the per-tenant Valkey stream key.

    Mirrors :func:`meho_backplane.api.v1.feed._stream_key` and
    :func:`meho_backplane.broadcast.publisher` exactly so the UI
    bridge reads the same key the publisher writes. Takes the tenant
    id positionally (a :class:`uuid.UUID` in practice) and stringifies
    it -- the f-string formatting matches the publisher's.
    """
    return f"meho:feed:{tenant_id}"


async def _ui_feed_generator(
    *,
    tenant_id: object,
    operator_sub: str,
    cursor: str,
    op_class: str | None,
    principal: str | None,
    target: str | None,
) -> AsyncIterator[str]:
    """SSE generator scoped to the session's tenant.

    Structurally identical to
    :func:`meho_backplane.api.v1.feed._feed_generator` -- BLOCK on
    XREAD, delegate parse + filter to the shared
    :func:`_process_entries`, emit a heartbeat on outbound silence so
    HTTP intermediaries don't idle-time-out the connection. The cursor
    advances past every consumed entry (not only the ones that survive
    the filter) so a busy-but-filtered tenant doesn't re-read the same
    batch under explicit-cursor replay.

    On client disconnect Starlette raises
    :class:`asyncio.CancelledError` into the pending ``xread`` await;
    we log and re-raise per the asyncio cancellation contract (Sonar
    S7497 -- swallowing it breaks the task tree's unwind invariants).
    """
    client = get_broadcast_client()
    stream_key = _stream_key(tenant_id)
    last_heartbeat = time.monotonic()

    try:
        while True:
            entries = await client.xread(
                {stream_key: cursor},
                block=_XREAD_BLOCK_MS,
                count=_XREAD_COUNT,
            )
            now = time.monotonic()
            emitted_any = False
            if entries:
                # redis-py returns ``[[stream_key, [(entry_id, fields), ...]], ...]``;
                # we query exactly one stream, so ``entries[0][1]`` is
                # this tenant's ``(entry_id, fields_dict)`` list.
                _key, items = entries[0]
                if items:
                    # Advance past EVERY consumed entry before the yield
                    # loop so a fully-filtered batch still moves the
                    # cursor forward -- mirrors the API route's M2 fix.
                    cursor = items[-1][0]
                for _entry_id, frame in _process_entries(
                    items,
                    op_class=op_class,
                    principal=principal,
                    target=target,
                    stream_key=stream_key,
                ):
                    yield frame
                    emitted_any = True
            if emitted_any:
                last_heartbeat = now
            elif now - last_heartbeat >= _HEARTBEAT_INTERVAL_SECONDS:
                yield ": heartbeat\n\n"
                last_heartbeat = now
    except asyncio.CancelledError:
        _log.info(
            "ui_broadcast_stream_disconnected",
            stream_key=stream_key,
            operator_sub=operator_sub,
        )
        raise


def build_stream_router() -> APIRouter:
    """Construct the broadcast SSE-bridge :class:`APIRouter`.

    Registers ``GET /ui/broadcast/stream`` -- the session-gated SSE
    source the live feed subscribes to. The route name
    (``ui_broadcast_stream``) is referenced by the feed template's
    ``sse-connect`` URL; a rename here must update the template in
    lockstep.
    """
    router = APIRouter(tags=["ui-broadcast"])

    async def _handler(
        request: Request,
        op_class: str | None = Query(default=None, max_length=64),
        principal: str | None = Query(default=None, max_length=256),
        target: str | None = Query(default=None, max_length=256),
        since: str | None = Query(
            default=None,
            description="Explicit replay cursor; superseded by Last-Event-Id when present.",
        ),
        session_ctx: UISessionContext = _require_ui_session_dep,
    ) -> StreamingResponse:
        """``GET /ui/broadcast/stream`` -- stream the session tenant's feed.

        Tenant is taken from the validated session, never a query
        parameter. ``Last-Event-Id`` (sent by ``EventSource`` on
        reconnect) takes precedence over ``since`` for replay.
        """
        cursor = _validate_cursor_or_400(
            _resolve_cursor(
                last_event_id_header=request.headers.get("Last-Event-Id"),
                since=since,
            )
        )
        generator = _ui_feed_generator(
            tenant_id=session_ctx.tenant_id,
            operator_sub=session_ctx.operator_sub,
            cursor=cursor,
            op_class=op_class,
            principal=principal,
            target=target,
        )
        return StreamingResponse(
            generator,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "X-Accel-Buffering": "no",
            },
        )

    router.add_api_route(
        "/ui/broadcast/stream",
        _handler,
        methods=["GET"],
        name="ui_broadcast_stream",
        response_class=StreamingResponse,
    )
    return router
