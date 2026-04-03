# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Alertmanager Connector.

Extends ObservabilityHTTPConnector for Alertmanager v2 API access with handler
mixins for alert listing, silence CRUD, and cluster status monitoring.

9 operations across 3 categories:
- Alerts: list_alerts, get_firing_alerts, get_alert_detail (3 READ)
- Silences: list_silences, create_silence, silence_alert, expire_silence (1 READ + 3 WRITE)
- Status: get_cluster_status, list_receivers (2 READ)

Example:
    connector = AlertmanagerConnector(
        connector_id="abc123",
        config={
            "base_url": "http://alertmanager:9093",
            "auth_type": "none",
        },
        credentials={},
    )

    async with connector:
        ok = await connector.test_connection()
        result = await connector.execute("list_alerts", {"state": "active"})
"""

import time
from collections.abc import Callable
from typing import Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.alertmanager.handlers import (
    AlertHandlerMixin,
    SilenceHandlerMixin,
    StatusHandlerMixin,
)
from meho_app.modules.connectors.alertmanager.operations import ALERTMANAGER_OPERATIONS
from meho_app.modules.connectors.base import (
    OperationDefinition,
    OperationResult,
    TypeDefinition,
)
from meho_app.modules.connectors.observability.base import ObservabilityHTTPConnector

logger = get_logger(__name__)


class AlertmanagerConnector(
    ObservabilityHTTPConnector,
    AlertHandlerMixin,
    SilenceHandlerMixin,
    StatusHandlerMixin,
):
    """
    Alertmanager connector using httpx for native Alertmanager v2 HTTP API access.

    Provides 9 pre-defined operations across three categories:
    - Alerts (list_alerts, get_firing_alerts, get_alert_detail) -- 3 ops
    - Silences (list_silences, create_silence, silence_alert, expire_silence) -- 4 ops
    - Status (get_cluster_status, list_receivers) -- 2 ops

    No topology entities -- alerts are ephemeral, not infrastructure.
    No multi-tenant header -- Alertmanager does not use X-Scope-OrgID.
    """

    def __init__(
        self,
        connector_id: str,
        config: dict[str, Any],
        credentials: dict[str, Any],
    ) -> None:
        super().__init__(connector_id, config, credentials)

        # Alertmanager version (detected on test_connection)
        self.alertmanager_version: str | None = None

        # Build operation dispatch table from handler mixins
        self._operation_handlers: dict[str, Callable] = self._build_operation_handlers()

    # =========================================================================
    # CONNECTION & EXECUTION
    # =========================================================================

    async def test_connection(self) -> bool:
        """
        Test connection via /api/v2/status with /-/ready fallback.

        Tries /api/v2/status first (returns cluster/version info). If that
        fails, falls back to /-/ready which returns HTTP 200. Stores version
        from status response if available.
        """
        # Try /api/v2/status first -- provides version and cluster info
        try:
            data = await self._api_get("/api/v2/status")
            # Alertmanager v2 status returns versionInfo.version
            version_info = data.get("versionInfo", {})
            version = version_info.get("version")
            if version:
                self.alertmanager_version = version
            logger.info(
                f"Alertmanager connection verified via /api/v2/status: {self.base_url} "
                f"(version: {self.alertmanager_version})"
            )
            return True
        except Exception as e:
            logger.debug(f"Alertmanager /api/v2/status failed, trying /-/ready: {e}")

        # Fallback: /-/ready endpoint (returns HTTP 200)
        try:
            if not self._client:
                await self.connect()
            assert self._client is not None  # noqa: S101 -- runtime assertion for invariant checking

            response = await self._client.get("/-/ready")
            response.raise_for_status()
            logger.info(f"Alertmanager connection verified via /-/ready: {self.base_url}")
            return True
        except Exception as e:
            logger.warning(f"Alertmanager connection test failed: {e}")
            return False

    async def _execute_operation(
        self,
        operation_id: str,
        parameters: dict[str, Any],
    ) -> OperationResult:
        """Execute an Alertmanager operation."""
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
            # Alerts (3)
            "list_alerts": self._list_alerts_handler,
            "get_firing_alerts": self._get_firing_alerts_handler,
            "get_alert_detail": self._get_alert_detail_handler,
            # Silences (4)
            "list_silences": self._list_silences_handler,
            "create_silence": self._create_silence_handler,
            "silence_alert": self._silence_alert_handler,
            "expire_silence": self._expire_silence_handler,
            # Status (2)
            "get_cluster_status": self._get_cluster_status_handler,
            "list_receivers": self._list_receivers_handler,
        }

    def get_operations(self) -> list[OperationDefinition]:
        """Get Alertmanager operations for registration."""
        return list(ALERTMANAGER_OPERATIONS)

    def get_types(self) -> list[TypeDefinition]:
        """Get Alertmanager types for registration.

        Returns empty list -- alerts are ephemeral, not topology entities.
        """
        return []

    # =========================================================================
    # ALERTMANAGER API HELPERS
    # =========================================================================

    async def _api_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """
        GET request to Alertmanager API.

        Args:
            path: API path (e.g., '/api/v2/alerts', '/api/v2/status').
            params: Optional query parameters.

        Returns:
            Parsed JSON response data.
        """
        if not self._client:
            await self.connect()
        assert self._client is not None  # noqa: S101 -- runtime assertion for invariant checking

        response = await self._client.get(path, params=params)
        response.raise_for_status()
        return response.json()

    async def _api_post(self, path: str, json_body: Any) -> Any:
        """
        POST request to Alertmanager API.

        Used primarily for creating silences via POST /api/v2/silences.

        Args:
            path: API path (e.g., '/api/v2/silences').
            json_body: JSON-serializable request body.

        Returns:
            Parsed JSON response data.
        """
        if not self._client:
            await self.connect()
        assert self._client is not None  # noqa: S101 -- runtime assertion for invariant checking

        response = await self._client.post(path, json=json_body)
        response.raise_for_status()
        return response.json()

    async def _api_delete(self, path: str) -> None:
        """
        DELETE request to Alertmanager API.

        Used for expiring silences via DELETE /api/v2/silence/{silenceID}.

        Args:
            path: API path (e.g., '/api/v2/silence/{silenceID}').
        """
        if not self._client:
            await self.connect()
        assert self._client is not None  # noqa: S101 -- runtime assertion for invariant checking

        response = await self._client.delete(path)
        response.raise_for_status()
