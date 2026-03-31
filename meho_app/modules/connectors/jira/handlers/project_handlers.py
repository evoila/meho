# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Jira Project Handler Mixin.

Handles project listing and discovery for the agent.
"""

from typing import Any


class ProjectHandlerMixin:
    """Mixin for Jira project operations: list projects."""

    # These will be provided by JiraConnector (base class)
    async def _get(self, path: str, params: dict | None = None) -> dict: ...

    # =========================================================================
    # HANDLER METHODS
    # =========================================================================

    async def _list_projects_handler(self, params: dict[str, Any]) -> dict:
        """
        List accessible Jira projects.

        Uses GET /rest/api/3/project/search with optional name query.
        Returns project key, name, type, and style for discovery.
        """
        query_params: dict[str, Any] = {}

        max_results = params.get("max_results", 50)
        query_params["maxResults"] = max_results

        search = params.get("search")
        if search:
            query_params["query"] = search

        data = await self._get("/rest/api/3/project/search", params=query_params)

        values = data.get("values", [])
        projects = []
        for project in values:
            projects.append(
                {
                    "key": project.get("key", ""),
                    "name": project.get("name", ""),
                    "type": project.get("projectTypeKey", ""),
                    "style": project.get("style", ""),
                }
            )

        return {
            "total": data.get("total", len(projects)),
            "projects": projects,
        }
