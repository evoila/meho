# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
ArgoCD History Handler Mixin.

Handles sync history and revision metadata inspection.
"""

from typing import Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.argocd.serializers import (
    serialize_revision_metadata,
    serialize_sync_history,
)

logger = get_logger(__name__)


class HistoryHandlerMixin:
    """Mixin for ArgoCD history operations: sync history, revision metadata."""

    # These will be provided by ArgoConnector (base class)
    async def _get(self, path: str, params: dict | None = None) -> dict: ...

    # =========================================================================
    # HANDLER METHODS
    # =========================================================================

    async def _get_sync_history_handler(self, params: dict[str, Any]) -> list[dict]:
        """
        Get sync/deployment history for an application.

        Fetches the full application (history is embedded in status.history)
        and serializes the history entries with deployment IDs for rollback.
        """
        app_name = params["application"]
        max_entries = params.get("max_entries", 10)
        query_params: dict[str, Any] = {}

        app_namespace = params.get("app_namespace")
        if app_namespace:
            query_params["appNamespace"] = app_namespace

        # History is embedded in the application status -- fetch full app
        data = await self._get(
            f"/api/v1/applications/{app_name}",
            params=query_params or None,
        )
        return serialize_sync_history(data, max_entries=max_entries)

    async def _get_revision_metadata_handler(self, params: dict[str, Any]) -> dict:
        """
        Get revision metadata (commit message, author, date).

        Uses GET /api/v1/applications/{name}/revisions/{revision}/metadata.
        """
        app_name = params["application"]
        revision = params["revision"]
        query_params: dict[str, Any] = {}

        app_namespace = params.get("app_namespace")
        if app_namespace:
            query_params["appNamespace"] = app_namespace

        data = await self._get(
            f"/api/v1/applications/{app_name}/revisions/{revision}/metadata",
            params=query_params or None,
        )
        return serialize_revision_metadata(data)
