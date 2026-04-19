# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GitHub Pull Request Handler Mixin.

Handles listing and getting pull request details.
"""

from typing import Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.github.serializers import serialize_pull_request

logger = get_logger(__name__)


class PRHandlerMixin:
    """Mixin for GitHub pull request operations: list and get."""

    # These will be provided by GitHubConnector (base class)
    async def _get(self, path: str, params: dict | None = None) -> dict: ...  # type: ignore[empty-body]

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

    async def _list_pull_requests_handler(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """
        List pull requests in a repository.

        Uses GET /repos/{org}/{repo}/pulls with optional state filter.
        """
        repo = params["repo"]
        state = params.get("state", "all")

        query_params: dict[str, Any] = {"state": state}

        prs = await self._get_paginated(
            f"/repos/{self.organization}/{repo}/pulls",
            params=query_params,
        )
        return [serialize_pull_request(pr) for pr in prs]

    async def _get_pull_request_handler(self, params: dict[str, Any]) -> dict:
        """
        Get detailed pull request information.

        Uses GET /repos/{org}/{repo}/pulls/{pull_number}.
        """
        repo = params["repo"]
        pull_number = params["pull_number"]

        data = await self._get(
            f"/repos/{self.organization}/{repo}/pulls/{pull_number}",
        )
        return serialize_pull_request(data)
