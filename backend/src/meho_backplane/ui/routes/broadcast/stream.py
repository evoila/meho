# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /ui/broadcast/stream`` -- the session-gated SSE bridge.

Initiative #338 (G10.1 Activity broadcast UI), Task #867. The browser
source the broadcast live-feed view and the wall monitor subscribe to
through the HTMX ``sse`` extension.

Why a UI-owned stream instead of ``/api/v1/feed``
=================================================

``GET /api/v1/feed`` authenticates with ``Authorization: Bearer <jwt>``.
The browser's ``EventSource`` cannot set request headers (the WHATWG
constructor accepts only ``withCredentials``), so it sends cookies, not
a Bearer token. This route lives under ``/ui/`` where
:class:`~meho_backplane.ui.auth.middleware.UISessionMiddleware` gates it
with the BFF session cookie. The tenant comes from the validated
session, never from a query parameter.

What it streams (#3758)
=======================

Deliberate agent announcements only, read from the announcement
archive by :func:`~meho_backplane.api.v1.feed.announcement_feed_frames`
-- the same generator ``/api/v1/feed`` uses, so the two surfaces send
the same frames and a ``Last-Event-Id`` from one resumes on the other.
Automatic activity is not on Broadcast; the dashboard and connector
cards read recent operations from the audit log instead, and the
approvals bell reads the approvals badge.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from meho_backplane.api.v1.feed import (
    announcement_feed_frames,
    feed_response,
    resolve_feed_start,
)
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast.archive_reader import AnnouncementFilter
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session

__all__ = ["build_stream_router", "session_reader"]

#: Module-level :class:`fastapi.Depends` closure (ruff B008).
_require_ui_session_dep = Depends(require_ui_session)


def session_reader(session_ctx: UISessionContext) -> Operator:
    """Build the reader identity the archive helpers take from a UI session.

    The archive reads use only ``tenant_id`` (the scope) and ``sub`` (log
    lines). The session middleware already authenticated the request; the
    role on this value is not used for any decision.
    """
    return Operator(
        sub=session_ctx.operator_sub,
        raw_jwt="ui-session",
        tenant_id=session_ctx.tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


def build_stream_router() -> APIRouter:
    """Construct the broadcast SSE-bridge :class:`APIRouter`.

    Registers ``GET /ui/broadcast/stream``. The path is referenced by the
    broadcast feed and wall templates' ``sse-connect`` URLs; a rename here
    must update them in lockstep.
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
        """``GET /ui/broadcast/stream`` -- stream the session tenant's announcements."""
        reader = session_reader(session_ctx)
        start = resolve_feed_start(reader, request.headers.get("Last-Event-Id"), since)
        flt = AnnouncementFilter(op_class=op_class, principal=principal, target=target)
        return feed_response(announcement_feed_frames(reader, start=start, flt=flt, surface="ui"))

    router.add_api_route(
        "/ui/broadcast/stream",
        _handler,
        methods=["GET"],
        name="ui_broadcast_stream",
        response_class=StreamingResponse,
    )
    return router
