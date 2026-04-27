# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Internal module routes for the audit module.

Provides a health/status endpoint for debugging.
The BFF routes (consumed by the frontend) live in meho_app/api/routes_audit.py.
"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def audit_health() -> dict:
    """Health check for audit module."""
    return {"module": "audit", "status": "ok"}
