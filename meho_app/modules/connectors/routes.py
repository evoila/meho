# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Connectors Service Routes - Direct HTTP endpoints (if needed).

Note: Most operations go through BFF (meho_api).
This service primarily provides repository/database access.
"""

from fastapi import APIRouter

from meho_app.core.otel import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/connectors", tags=["connectors"])


@router.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint"""
    return {"status": "healthy", "service": "meho-connectors"}
