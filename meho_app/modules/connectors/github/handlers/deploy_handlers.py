# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GitHub Deployment Handler Mixin.

Handles listing deployments and getting commit status checks.
"""

from typing import Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.github.serializers import (
    serialize_commit_status,
    serialize_deployment,
)

logger = get_logger(__name__)


class DeployHandlerMixin:
    """Mixin for GitHub deployment and commit status operations."""

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

    async def _list_deployments_handler(self, params: dict[str, Any]) -> dict:
        """
        List deployments for a repository with status history.

        Uses GET /repos/{org}/{repo}/deployments with optional environment filter.
        For each deployment, fetches the most recent statuses (first page, 5 items)
        to show current deployment state.
        """
        repo = params["repo"]
        query_params: dict[str, Any] = {}

        environment = params.get("environment")
        if environment:
            query_params["environment"] = environment

        deployments = await self._get_paginated(
            f"/repos/{self.organization}/{repo}/deployments",
            params=query_params,
        )

        # Fetch statuses for each deployment (first page only, most recent)
        serialized = []
        for deployment in deployments:
            deploy_id = deployment.get("id")
            try:
                statuses_data = await self._get(
                    f"/repos/{self.organization}/{repo}/deployments/{deploy_id}/statuses",
                    params={"per_page": 5},
                )
                # Statuses endpoint returns a list directly
                statuses = statuses_data if isinstance(statuses_data, list) else []
            except Exception as e:
                logger.warning(f"Failed to fetch statuses for deployment {deploy_id}: {e}")
                statuses = []

            serialized.append(serialize_deployment(deployment, statuses=statuses))

        return serialized

    async def _get_commit_status_handler(self, params: dict[str, Any]) -> dict:
        """
        Get combined CI/CD status for a commit.

        Makes TWO calls to merge both CI status systems:
        1. GET /repos/{org}/{repo}/commits/{ref}/status -- legacy commit statuses
        2. GET /repos/{org}/{repo}/commits/{ref}/check-runs -- GitHub Actions checks

        This is critical because GitHub has two separate systems (Pitfall 5
        from research). Missing either gives an incomplete picture.
        """
        repo = params["repo"]
        ref = params["ref"]

        # Fetch both status systems
        combined_status = await self._get(
            f"/repos/{self.organization}/{repo}/commits/{ref}/status",
        )
        check_runs = await self._get(
            f"/repos/{self.organization}/{repo}/commits/{ref}/check-runs",
        )

        return serialize_commit_status(combined_status, check_runs)
