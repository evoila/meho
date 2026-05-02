# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Tempo Discovery Handler Mixin.

Handles tag listing and tag value enumeration for trace filtering.
Uses Tempo HTTP API endpoints for tag discovery.
"""

from typing import Any


class DiscoveryHandlerMixin:
    """Mixin for Tempo discovery handlers."""

    # These will be provided by TempoConnector (base class)
    async def _get_tags(self) -> list: ...  # type: ignore[empty-body]

    async def _get_tag_values(self, tag: str) -> list: ...  # type: ignore[empty-body]

    async def _list_tags_handler(self, _params: dict[str, Any]) -> dict:
        """List available trace tags in Tempo."""
        tags = await self._get_tags()
        sorted_tags = sorted(tags) if tags else []

        return {
            "tags": sorted_tags,
            "count": len(sorted_tags),
        }

    async def _list_tag_values_handler(self, params: dict[str, Any]) -> dict:
        """Get all values for a specific trace tag."""
        tag = params["tag"]
        values = await self._get_tag_values(tag)
        sorted_values = sorted(values) if values else []

        return {
            "tag": tag,
            "values": sorted_values,
            "count": len(sorted_values),
        }
