# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Confluence Space Handler Mixin.

Handles space listing and discovery for the agent.
"""

from typing import Any


class SpaceHandlerMixin:
    """Mixin for Confluence space operations: list spaces."""

    # These will be provided by ConfluenceConnector (base class)
    async def _get(self, path: str, params: dict | None = None) -> dict: ...  # type: ignore[empty-body]

    base_url: str

    # =========================================================================
    # HANDLER METHODS
    # =========================================================================

    async def _list_spaces_handler(self, params: dict[str, Any]) -> dict:
        """
        List accessible Confluence spaces.

        Uses v2 API GET /wiki/api/v2/spaces. Returns space id, key,
        name, type, and URL for discovery.
        """
        max_results = params.get("max_results", 25)

        data = await self._get(
            "/wiki/api/v2/spaces",
            params={"limit": max_results},
        )

        results = data.get("results", [])
        spaces = []
        for space in results:
            spaces.append(
                {
                    "id": space.get("id"),
                    "key": space.get("key", ""),
                    "name": space.get("name", ""),
                    "type": space.get("type", ""),
                    "url": f"{self.base_url}/wiki/spaces/{space.get('key', '')}",
                }
            )

        return {
            "total": len(spaces),
            "spaces": spaces,
        }
