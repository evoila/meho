# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Broadcast UI routes: live feed page + SSE bridge + filters + drawer.

Initiative #338 (G10.1 Activity broadcast UI). Task #867 (G10.1-T1)
shipped the live feed; Task #868 (G10.1-T2) adds the filter bar, the
event-detail drawer, and the PII 🔒 visualisation. Wall-monitor mode +
the Last-24h replay tab land in T3 (#869).

Module layout:

* :mod:`~meho_backplane.ui.routes.broadcast.feed` -- the
  ``GET /ui/broadcast`` (full page) and ``GET /ui/broadcast/feed``
  (filtered fragment) routes. The page renders the live-feed view (SSE
  wrapper + filter bar + drawer slot + empty state + server-rendered
  event-row ``<template>`` + the Alpine 1000-row cap); the fragment
  route is the filter-submit target that re-renders the feed with the
  active server-side filters baked into a fresh ``sse-connect`` URL.
* :mod:`~meho_backplane.ui.routes.broadcast.stream` -- the
  ``GET /ui/broadcast/stream`` route. A UI-session-gated SSE source the
  feed view subscribes to; bridges the BFF session cookie to the
  tenant-scoped Valkey feed because the browser ``EventSource`` cannot
  send the Bearer header ``/api/v1/feed`` (G6.1-T4) requires.
* :mod:`~meho_backplane.ui.routes.broadcast.event` -- the
  ``GET /ui/broadcast/event/{audit_id}`` route. Renders the event
  detail drawer fragment (full audit row payload + request_id +
  audit_id + broadcast event_id) the feed rows open via ``hx-get``;
  applies the same decision-#3 aggregate-only gate as the publisher so
  a sensitive op never leaks its payload on click.

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

from meho_backplane.ui.routes.broadcast.event import build_event_router
from meho_backplane.ui.routes.broadcast.feed import build_feed_router
from meho_backplane.ui.routes.broadcast.stream import build_stream_router

__all__ = ["build_router"]


def build_router() -> APIRouter:
    """Aggregate the broadcast UI routes into one ``/ui/broadcast*`` router.

    Factory function (not a module-level constant) so a test app can
    construct multiple parallel routers without sharing route state --
    mirrors the chassis convention in
    :mod:`meho_backplane.ui.routes.topology`.

    The feed router is included **before** the event router so the
    literal ``/ui/broadcast/feed`` fragment path is matched ahead of
    the parametrised ``/ui/broadcast/event/{audit_id}`` -- they share
    no overlapping segment, but declaration order is the contract
    FastAPI resolves on, so keeping the literal route first is the safe
    discipline (mirrors the targets router's ``/discover`` ahead of
    ``/{name}``).
    """
    router = APIRouter()
    router.include_router(build_feed_router())
    router.include_router(build_stream_router())
    router.include_router(build_event_router())
    return router
