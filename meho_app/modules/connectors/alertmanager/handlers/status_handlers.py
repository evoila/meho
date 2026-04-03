# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Alertmanager Status Handler Mixin.

Handles cluster status and receiver listing for operational awareness.
"""

from typing import Any

from meho_app.modules.connectors.alertmanager.serializers import (
    serialize_cluster_status,
    serialize_receivers,
)


class StatusHandlerMixin:
    """Mixin for Alertmanager cluster status and receiver handlers."""

    # These will be provided by AlertmanagerConnector (base class)
    async def _api_get(self, path: str, params: dict | None = None) -> Any: ...

    # =========================================================================
    # HANDLER METHODS
    # =========================================================================

    async def _get_cluster_status_handler(self, _params: dict[str, Any]) -> dict:
        """
        Get Alertmanager cluster health.

        Returns cluster name, peer count, peer details, HA status.
        """
        status = await self._api_get("/api/v2/status")
        return serialize_cluster_status(status)

    async def _list_receivers_handler(self, _params: dict[str, Any]) -> dict:
        """
        List configured notification receivers.

        Returns receiver names from Alertmanager configuration.
        """
        receivers = await self._api_get("/api/v2/receivers")
        return serialize_receivers(receivers)
