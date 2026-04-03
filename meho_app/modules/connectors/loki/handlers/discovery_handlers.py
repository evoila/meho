# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Loki Discovery Handler Mixin.

Handles label listing and label value enumeration.
Uses Loki HTTP API endpoints for label discovery.
"""

from typing import Any

from meho_app.modules.connectors.observability.time_range import TimeRange


class DiscoveryHandlerMixin:
    """Mixin for Loki discovery handlers."""

    # These will be provided by LokiConnector (base class)
    async def _get_labels(self, start: str | None = None, end: str | None = None) -> list: ...  # type: ignore[empty-body]

    async def _get_label_values(  # type: ignore[empty-body]
        self, label: str, start: str | None = None, end: str | None = None
    ) -> list: ...

    async def _list_labels(self, params: dict[str, Any]) -> dict:
        """List available log labels in Loki."""
        time_range = TimeRange.from_relative(params.get("time_range", "1h"))
        start = str(int(time_range.start.timestamp()))
        end = str(int(time_range.end.timestamp()))

        labels = await self._get_labels(start=start, end=end)
        sorted_labels = sorted(labels) if labels else []

        return {
            "labels": sorted_labels,
            "count": len(sorted_labels),
        }

    async def _list_label_values(self, params: dict[str, Any]) -> dict:
        """Get all values for a specific log label."""
        label = params["label"]
        time_range = TimeRange.from_relative(params.get("time_range", "1h"))
        start = str(int(time_range.start.timestamp()))
        end = str(int(time_range.end.timestamp()))

        values = await self._get_label_values(label, start=start, end=end)
        sorted_values = sorted(values) if values else []

        return {
            "label": label,
            "values": sorted_values,
            "count": len(sorted_values),
        }
