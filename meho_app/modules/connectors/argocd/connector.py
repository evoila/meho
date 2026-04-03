# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
ArgoCD Connector.

Extends ArgoHTTPBase for ArgoCD REST API access with handler mixins for
application management, resource inspection, sync history, diffs, and
sync/rollback operations.

10 operations across 5 categories:
- Applications: list_applications, get_application (2 READ)
- Resources: get_resource_tree, get_managed_resources, get_application_events (3 READ)
- History: get_sync_history, get_revision_metadata (2 READ)
- Diff: get_server_diff (1 READ)
- Sync: sync_application (1 WRITE), rollback_application (1 DESTRUCTIVE)

Example:
    connector = ArgoConnector(
        connector_id="abc123",
        config={
            "base_url": "https://argocd.example.com",
            "verify_ssl": False,
        },
        credentials={
            "api_token": "your-argocd-token",
        },
    )

    async with connector:
        ok = await connector.test_connection()
        result = await connector.execute("list_applications", {"project": "default"})
"""

import time
from collections.abc import Callable
from typing import Any

from meho_app.core.otel import get_logger
from meho_app.core.otel.context import tenant_id_ctx
from meho_app.modules.connectors.argocd.base import ArgoHTTPBase
from meho_app.modules.connectors.argocd.handlers import (
    ApplicationHandlerMixin,
    DiffHandlerMixin,
    HistoryHandlerMixin,
    ResourceHandlerMixin,
    SyncHandlerMixin,
)
from meho_app.modules.connectors.argocd.operations import ARGOCD_OPERATIONS
from meho_app.modules.connectors.base import (
    OperationDefinition,
    OperationResult,
    TypeDefinition,
)
from meho_app.modules.topology.auto_discovery.queue import get_discovery_queue

logger = get_logger(__name__)


class ArgoConnector(
    ArgoHTTPBase,
    ApplicationHandlerMixin,
    ResourceHandlerMixin,
    HistoryHandlerMixin,
    DiffHandlerMixin,
    SyncHandlerMixin,
):
    """
    ArgoCD connector using httpx for ArgoCD REST API access.

    Provides 10 pre-defined operations across five categories:
    - Applications (list_applications, get_application) -- 2 ops
    - Resources (get_resource_tree, get_managed_resources, get_application_events) -- 3 ops
    - History (get_sync_history, get_revision_metadata) -- 2 ops
    - Diff (get_server_diff) -- 1 op
    - Sync (sync_application, rollback_application) -- 2 ops

    No topology entities via get_types -- ArgoCD Server entity is handled
    via topology schema separately.
    """

    def __init__(
        self,
        connector_id: str,
        config: dict[str, Any],
        credentials: dict[str, Any],
    ) -> None:
        super().__init__(connector_id, config, credentials)

        # ArgoCD server info (populated on test_connection)
        self.argocd_version: str | None = None
        self.app_count: int = 0

        # Build operation dispatch table from handler mixins
        self._operation_handlers: dict[str, Callable] = self._build_operation_handlers()

    # =========================================================================
    # CONNECTION & EXECUTION
    # =========================================================================

    async def test_connection(self) -> bool:
        """
        Test connection by validating auth and server access.

        1. GET /api/version -- confirms auth, stores ArgoCD version
        2. GET /api/v1/applications -- counts accessible applications
        """
        try:
            await self.connect()

            # Verify auth via /api/version
            version_data = await self._get("/api/version")
            self.argocd_version = version_data.get("Version", "unknown")

            # Verify application access
            apps_data = await self._get(
                "/api/v1/applications",
                params={"fields": "items.metadata.name"},
            )
            items = apps_data.get("items") or []
            self.app_count = len(items)

            logger.info(
                f"ArgoCD connection verified: {self.base_url} "
                f"(version: {self.argocd_version}, apps: {self.app_count})"
            )
            return True

        except Exception as e:
            logger.warning(f"ArgoCD connection test failed: {e}")
            return False

    async def _execute_operation(
        self,
        operation_id: str,
        parameters: dict[str, Any],
    ) -> OperationResult:
        """Execute an ArgoCD operation."""
        start_time = time.time()

        if not self._is_connected:
            await self.connect()

        handler = self._operation_handlers.get(operation_id)
        if not handler:
            return OperationResult(
                success=False,
                error=f"Unknown operation: {operation_id}",
                error_code="NOT_FOUND",
                operation_id=operation_id,
            )

        try:
            result = await handler(parameters)
            duration_ms = (time.time() - start_time) * 1000

            # Forward topology hints to discovery queue (best-effort)
            await self._forward_topology_hints()

            logger.info(f"{operation_id}: completed in {duration_ms:.1f}ms")

            return OperationResult(
                success=True,
                data=result,
                operation_id=operation_id,
                duration_ms=duration_ms,
            )
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            logger.error(f"{operation_id} failed: {e}", exc_info=True)

            error_code = self._map_http_error(e)

            return OperationResult(
                success=False,
                error=str(e),
                error_code=error_code,
                operation_id=operation_id,
                duration_ms=duration_ms,
            )

    def _build_operation_handlers(self) -> dict[str, Callable]:
        """Map operation IDs to handler methods from mixins."""
        return {
            # Applications (2)
            "list_applications": self._list_applications_handler,
            "get_application": self._get_application_handler,
            # Resources (3)
            "get_resource_tree": self._get_resource_tree_handler,
            "get_managed_resources": self._get_managed_resources_handler,
            "get_application_events": self._get_application_events_handler,
            # History (2)
            "get_sync_history": self._get_sync_history_handler,
            "get_revision_metadata": self._get_revision_metadata_handler,
            # Diff (1)
            "get_server_diff": self._get_server_diff_handler,
            # Sync (2)
            "sync_application": self._sync_application_handler,
            "rollback_application": self._rollback_application_handler,
        }

    async def _forward_topology_hints(self) -> None:
        """
        Forward accumulated _topology_hints to the discovery queue.

        Called after handler execution. Topology hints are stored on the
        instance by ResourceHandlerMixin._emit_managed_by_edges() and
        _emit_same_as_edges(). This method drains the list and pushes
        each message to the DiscoveryQueue singleton.

        Best-effort: failures are logged but never propagate.
        """
        hints = getattr(self, "_topology_hints", None)
        if not hints:
            return

        try:
            queue = get_discovery_queue()
            tenant_id = tenant_id_ctx.get() or ""
            for message in hints:
                message.tenant_id = tenant_id
                await queue.push(message)
            logger.info(
                f"Forwarded {len(hints)} topology hint(s) to discovery queue for tenant {tenant_id}"
            )
        except Exception as e:
            logger.warning(f"Failed to forward topology hints: {e}")
        finally:
            # Always clear hints after attempt to prevent re-sending
            self._topology_hints = []

    def get_operations(self) -> list[OperationDefinition]:
        """Get ArgoCD operations for registration."""
        return list(ARGOCD_OPERATIONS)

    def get_types(self) -> list[TypeDefinition]:
        """Get ArgoCD types for registration.

        Returns empty list -- ArgoCD entities are handled via topology
        schema, not connector types.
        """
        return []
