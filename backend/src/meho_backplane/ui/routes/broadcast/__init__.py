# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Broadcast UI routes: live feed page + session-gated SSE bridge.

Initiative #338 (G10.1 Activity broadcast UI), Task #867 (G10.1-T1).
This subpackage ships the live activity feed surface the chassis stub
(#866) placeholders for. Filters + the event-detail drawer + the PII
visualisation land in T2 (#868); wall-monitor mode + the Last-24h
replay tab land in T3 (#869).

Module layout:

* :mod:`~meho_backplane.ui.routes.broadcast.feed` -- the
  ``GET /ui/broadcast`` route. Renders the full-page live-feed view
  (SSE wrapper + empty state + server-rendered event-row ``<template>``
  + the Alpine 1000-row cap).
* :mod:`~meho_backplane.ui.routes.broadcast.stream` -- the
  ``GET /ui/broadcast/stream`` route. A UI-session-gated SSE source the
  feed view subscribes to; bridges the BFF session cookie to the
  tenant-scoped Valkey feed because the browser ``EventSource`` cannot
  send the Bearer header ``/api/v1/feed`` (G6.1-T4) requires.

The umbrella :func:`build_router` aggregates both. It is mounted
**before** :func:`meho_backplane.ui.routes.stubs.build_stubs_router` in
:func:`meho_backplane.ui.routes.build_router` so the real
``/ui/broadcast`` handler wins the first-match-wins lookup; the
``broadcast`` stub is also removed from the stubs enumeration once this
router lands (a stub registration would otherwise shadow the real route
in the generated OpenAPI schema -- the same discipline T5 applied to the
topology path).
"""

from __future__ import annotations

from fastapi import APIRouter

from meho_backplane.ui.routes.broadcast.feed import build_feed_router
from meho_backplane.ui.routes.broadcast.stream import build_stream_router

__all__ = ["build_router"]


def build_router() -> APIRouter:
    """Aggregate the broadcast UI routes into one ``/ui/broadcast*`` router.

    Factory function (not a module-level constant) so a test app can
    construct multiple parallel routers without sharing route state --
    mirrors the chassis convention in
    :mod:`meho_backplane.ui.routes.topology`.
    """
    router = APIRouter()
    router.include_router(build_feed_router())
    router.include_router(build_stream_router())
    return router
