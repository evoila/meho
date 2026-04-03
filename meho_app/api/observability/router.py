# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Observability API router aggregation.

Combines all observability-related routes into a single router.
The router has a /observability prefix, and all sub-routes are relative to this.

Part of TASK-186: Deep Observability & Introspection System.
"""

from fastapi import APIRouter

from meho_app.api.observability import (
    router_events,
    router_explain,
    router_export,
    router_retention,
    router_sessions,
    router_transcripts,
)

# Create main router with prefix
router = APIRouter(prefix="/observability", tags=["observability"])

# Include all sub-routers
# Sessions: GET /sessions
router.include_router(router_sessions.router)

# Transcripts: GET /sessions/{id}/transcript, GET /sessions/{id}/summary
router.include_router(router_transcripts.router)

# Events: GET /sessions/{id}/events/{event_id}, GET /sessions/{id}/llm-calls,
#         GET /sessions/{id}/http-calls, GET /sessions/{id}/sql-queries, GET /search
router.include_router(router_events.router)

# Explain: GET /sessions/{id}/explain
router.include_router(router_explain.router)

# Retention: GET /retention/stats, POST /retention/cleanup
router.include_router(router_retention.router)

# Export: GET /export/sessions/{id}, POST /export/bulk
router.include_router(router_export.router)
