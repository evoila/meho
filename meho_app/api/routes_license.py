# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
License status endpoint -- public, no authentication required.

Phase 80: Returns edition status and available features so the frontend
can adapt its UI. This endpoint MUST NOT require authentication (Pitfall 3).
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from meho_app.core.licensing import LicenseService, get_license_service

router = APIRouter()


@router.get("/license")
async def get_license_info(
    license_svc: Annotated[LicenseService, Depends(get_license_service)],
) -> Any:
    """
    Return current edition and feature information.

    Public endpoint (no auth). Returns:
    - edition: "community" or "enterprise"
    - features: list of enabled enterprise features
    - org: organization name or null
    - expires_at: ISO 8601 expiry or null
    - in_grace_period: whether license is in post-expiry grace period
    """
    return license_svc.to_api_response()
