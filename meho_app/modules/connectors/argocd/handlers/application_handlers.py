# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
ArgoCD Application Handler Mixin.

Handles listing and getting detailed application status.
"""

from typing import Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.argocd.serializers import (
    serialize_application_detail,
    serialize_application_summary,
)

logger = get_logger(__name__)


class ApplicationHandlerMixin:
    """Mixin for ArgoCD application operations: list and get applications."""

    # These will be provided by ArgoConnector (base class)
    async def _get(self, path: str, params: dict | None = None) -> dict: ...

    # =========================================================================
    # HANDLER METHODS
    # =========================================================================

    async def _list_applications_handler(self, params: dict[str, Any]) -> list[dict]:
        """
        List ArgoCD applications with sync/health status.

        Uses GET /api/v1/applications with optional project and label filters.
        Returns serialized application summaries.
        """
        query_params: dict[str, Any] = {}

        project = params.get("project")
        if project:
            query_params["projects"] = [project]

        selector = params.get("selector")
        if selector:
            query_params["selector"] = selector

        data = await self._get(
            "/api/v1/applications",
            params=query_params or None,
        )
        apps = data.get("items") or []
        return [serialize_application_summary(app) for app in apps]

    async def _get_application_handler(self, params: dict[str, Any]) -> dict:
        """
        Get detailed ArgoCD application status.

        Uses GET /api/v1/applications/{name} with optional appNamespace.
        Returns serialized application detail including conditions,
        operation state, images, and resource count.
        """
        app_name = params["application"]
        query_params: dict[str, Any] = {}

        app_namespace = params.get("app_namespace")
        if app_namespace:
            query_params["appNamespace"] = app_namespace

        data = await self._get(
            f"/api/v1/applications/{app_name}",
            params=query_params or None,
        )
        return serialize_application_detail(data)
