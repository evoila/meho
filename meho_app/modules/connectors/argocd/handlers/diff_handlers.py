# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
ArgoCD Diff Handler Mixin.

Handles server-side diff (live vs desired state).
"""

from typing import Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.argocd.serializers import serialize_server_diff

logger = get_logger(__name__)


class DiffHandlerMixin:
    """Mixin for ArgoCD diff operations: server-side diff."""

    # These will be provided by ArgoConnector (base class)
    async def _get(self, path: str, params: dict | None = None) -> dict: ...  # type: ignore[empty-body]

    # =========================================================================
    # HANDLER METHODS
    # =========================================================================

    async def _get_server_diff_handler(self, params: dict[str, Any]) -> list[dict]:
        """
        Get server-side diff showing live vs desired state differences.

        Uses GET /api/v1/applications/{appName}/server-side-diff.
        Shows what a sync would change before triggering it.
        """
        app_name = params["application"]
        query_params: dict[str, Any] = {}

        app_namespace = params.get("app_namespace")
        if app_namespace:
            query_params["appNamespace"] = app_namespace

        data = await self._get(
            f"/api/v1/applications/{app_name}/server-side-diff",
            params=query_params or None,
        )
        return serialize_server_diff(data)
