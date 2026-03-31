# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
ArgoCD Sync Handler Mixin.

Handles sync and rollback operations (WRITE and DESTRUCTIVE).
"""

from typing import Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.argocd.serializers import serialize_sync_result

logger = get_logger(__name__)


class SyncHandlerMixin:
    """Mixin for ArgoCD sync operations: sync and rollback."""

    # These will be provided by ArgoConnector (base class)
    async def _post(self, path: str, json: Any = None) -> dict: ...

    # =========================================================================
    # HANDLER METHODS
    # =========================================================================

    async def _sync_application_handler(self, params: dict[str, Any]) -> dict:
        """
        Trigger ArgoCD sync to apply desired state from git.

        Uses POST /api/v1/applications/{name}/sync.
        Supports revision targeting, prune (destructive), and dry-run.
        """
        app_name = params["application"]
        prune = params.get("prune", False)
        dry_run = params.get("dry_run", False)
        revision = params.get("revision")

        payload: dict[str, Any] = {
            "prune": prune,
            "dryRun": dry_run,
        }
        if revision:
            payload["revision"] = revision

        # appNamespace as query param for non-default namespace apps
        query_suffix = ""
        app_namespace = params.get("app_namespace")
        if app_namespace:
            query_suffix = f"?appNamespace={app_namespace}"

        data = await self._post(
            f"/api/v1/applications/{app_name}/sync{query_suffix}",
            json=payload,
        )
        return serialize_sync_result(data)

    async def _rollback_application_handler(self, params: dict[str, Any]) -> dict:
        """
        Roll back application to a previous deployment.

        Uses POST /api/v1/applications/{name}/rollback.
        deployment_id is an integer from sync history (NOT a git revision SHA).
        """
        app_name = params["application"]
        deployment_id = params["deployment_id"]

        payload = {"id": deployment_id}

        # appNamespace as query param for non-default namespace apps
        query_suffix = ""
        app_namespace = params.get("app_namespace")
        if app_namespace:
            query_suffix = f"?appNamespace={app_namespace}"

        data = await self._post(
            f"/api/v1/applications/{app_name}/rollback{query_suffix}",
            json=payload,
        )
        return serialize_sync_result(data)
