# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GitHub Repository Handler Mixin.

Handles listing repositories in the configured organization.
"""

from typing import Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.github.serializers import serialize_repository

logger = get_logger(__name__)


class RepoHandlerMixin:
    """Mixin for GitHub repository operations."""

    # These will be provided by GitHubConnector (base class)
    async def _get_paginated(  # type: ignore[empty-body]
        self,
        path: str,
        params: dict | None = None,
        max_pages: int = 5,
        per_page: int = 30,
    ) -> list: ...

    organization: str

    # =========================================================================
    # HANDLER METHODS
    # =========================================================================

    async def _list_repositories_handler(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """
        List repositories in the configured GitHub organization.

        Uses GET /orgs/{org}/repos with optional type and sort filters.
        Returns serialized repository summaries.
        """
        query_params: dict[str, Any] = {}

        repo_type = params.get("type", "all")
        if repo_type:
            query_params["type"] = repo_type

        sort = params.get("sort", "pushed")
        if sort:
            query_params["sort"] = sort

        repos = await self._get_paginated(
            f"/orgs/{self.organization}/repos",
            params=query_params,
        )
        return [serialize_repository(r) for r in repos]
