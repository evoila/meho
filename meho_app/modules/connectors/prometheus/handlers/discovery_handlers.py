# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Prometheus Discovery Handler Mixin.

Handles target listing, metric discovery, alerts, and alert rules.
Uses Prometheus HTTP API endpoints (not PromQL queries).
"""

from typing import Any

from meho_app.modules.connectors.prometheus.serializers import (
    serialize_alerts,
    serialize_metrics_metadata,
    serialize_rules,
    serialize_targets,
)


class DiscoveryHandlerMixin:
    """Mixin for Prometheus discovery handlers."""

    # These will be provided by PrometheusConnector (base class)
    async def _get(self, path: str, params: dict | None = None) -> dict: ...  # type: ignore[empty-body]

    async def _list_targets(self, _params: dict[str, Any]) -> dict:
        """List all Prometheus scrape targets with health status."""
        data = await self._get("/api/v1/targets")
        active_targets = data.get("data", {}).get("activeTargets", [])
        return serialize_targets(active_targets)

    async def _discover_metrics(self, params: dict[str, Any]) -> dict:
        """Discover available metrics grouped by type."""
        data = await self._get("/api/v1/metadata")
        raw_metadata = data.get("data", {})
        search = params.get("search")
        return serialize_metrics_metadata(raw_metadata, search)

    async def _list_alerts(self, _params: dict[str, Any]) -> dict:
        """List all active alerts."""
        data = await self._get("/api/v1/alerts")
        raw_alerts = data.get("data", {}).get("alerts", [])
        return serialize_alerts(raw_alerts)

    async def _list_alert_rules(self, params: dict[str, Any]) -> dict:
        """List all alert and recording rules."""
        data = await self._get("/api/v1/rules")
        raw_rules = data.get("data", {})
        rule_type = params.get("type")
        return serialize_rules(raw_rules, rule_type)
