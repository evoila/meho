# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GitHub Commit Handler Mixin.

Handles listing commits and comparing refs.
"""

from typing import Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.github.serializers import (
    serialize_commit,
    serialize_comparison,
)

logger = get_logger(__name__)


class CommitHandlerMixin:
    """Mixin for GitHub commit operations: list and compare."""

    # These will be provided by GitHubConnector (base class)
    async def _get(self, path: str, params: dict | None = None) -> dict: ...

    async def _get_paginated(
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

    async def _list_commits_handler(self, params: dict[str, Any]) -> dict:
        """
        List recent commits on a repository branch.

        Uses GET /repos/{org}/{repo}/commits with optional sha (branch) filter.
        The `sha` param is how GitHub filters by branch name.
        """
        repo = params["repo"]
        query_params: dict[str, Any] = {}

        branch = params.get("branch")
        if branch:
            query_params["sha"] = branch  # GitHub uses 'sha' param for branch filter

        per_page = params.get("per_page")
        if per_page:
            query_params["per_page"] = per_page

        commits = await self._get_paginated(
            f"/repos/{self.organization}/{repo}/commits",
            params=query_params,
        )
        return [serialize_commit(c) for c in commits]

    async def _compare_refs_handler(self, params: dict[str, Any]) -> dict:
        """
        Compare two git refs (branches, tags, or SHAs).

        Uses GET /repos/{org}/{repo}/compare/{base}...{head}.
        Note the three-dot format in the URL (not two dots).
        """
        repo = params["repo"]
        base = params["base"]
        head = params["head"]

        data = await self._get(
            f"/repos/{self.organization}/{repo}/compare/{base}...{head}",
        )
        return serialize_comparison(data)
