# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Alertmanager Alert Handler Mixin.

Handles alert listing with filtering, firing alert shortcut, and
progressive disclosure via alert detail by fingerprint.
"""

from typing import Any

from meho_app.modules.connectors.alertmanager.serializers import (
    serialize_alert_detail,
    serialize_alerts,
)


class AlertHandlerMixin:
    """Mixin for Alertmanager alert listing, filtering, and detail handlers."""

    # These will be provided by AlertmanagerConnector (base class)
    async def _api_get(self, path: str, params: dict | None = None) -> Any: ...

    # =========================================================================
    # HANDLER METHODS
    # =========================================================================

    async def _list_alerts_handler(self, params: dict[str, Any]) -> dict:
        """
        List alerts with optional filters.

        Maps agent parameters to Alertmanager v2 query parameters:
        - active, silenced, inhibited: boolean query params
        - receiver: receiver query param
        - filter[]: label matchers for alertname, severity, etc.

        Groups alerts by alertname with summary header.
        """
        query_params: dict[str, Any] = {}

        # Boolean state filters
        active = params.get("active")
        if active is not None:
            query_params["active"] = str(active).lower()

        silenced = params.get("silenced")
        if silenced is not None:
            query_params["silenced"] = str(silenced).lower()

        inhibited = params.get("inhibited")
        if inhibited is not None:
            query_params["inhibited"] = str(inhibited).lower()

        # State shortcut: maps to active/silenced/inhibited booleans
        state = params.get("state")
        if state:
            if state == "active":
                query_params["active"] = "true"
                query_params["silenced"] = "false"
                query_params["inhibited"] = "false"
            elif state == "silenced":
                query_params["silenced"] = "true"
            elif state == "inhibited":
                query_params["inhibited"] = "true"

        # Receiver filter
        receiver = params.get("receiver")
        if receiver:
            query_params["receiver"] = receiver

        # Label matchers via filter[] param
        filters: list[str] = []
        severity = params.get("severity")
        if severity:
            filters.append(f'severity="{severity}"')

        alertname = params.get("alertname")
        if alertname:
            filters.append(f'alertname="{alertname}"')

        if filters:
            query_params["filter"] = filters

        alerts = await self._api_get("/api/v2/alerts", params=query_params or None)
        return serialize_alerts(alerts)

    async def _get_firing_alerts_handler(self, params: dict[str, Any]) -> dict:
        """
        Get only currently firing alerts (convenience shortcut).

        Delegates to _list_alerts_handler with active=True, silenced=False,
        inhibited=False pre-filled. Optional severity filter passthrough.
        """
        firing_params: dict[str, Any] = {
            "active": True,
            "silenced": False,
            "inhibited": False,
        }
        severity = params.get("severity")
        if severity:
            firing_params["severity"] = severity

        return await self._list_alerts_handler(firing_params)

    async def _get_alert_detail_handler(self, params: dict[str, Any]) -> dict:
        """
        Progressive disclosure for a single alert by fingerprint.

        Fetches all alerts and finds the matching fingerprint.
        Returns full labels, annotations, generatorURL, silenced_by, inhibited_by.
        """
        fingerprint = params["fingerprint"]

        # Fetch all alerts and find by fingerprint
        alerts = await self._api_get("/api/v2/alerts")

        for alert in alerts:
            if alert.get("fingerprint") == fingerprint:
                return serialize_alert_detail(alert)

        # Alert not found
        return {
            "error": f"Alert with fingerprint '{fingerprint}' not found",
            "hint": "The alert may have resolved. Use list_alerts to see current alerts.",
        }
