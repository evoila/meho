# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GitHub Connector.

Extends GitHubHTTPBase for GitHub REST API access with handler mixins for
repository listing, commits, pull requests, GitHub Actions (runs, jobs, logs),
deployments, and commit status checks.

12 operations across 6 categories:
- Repositories: list_repositories (1 READ)
- Commits: list_commits, compare_refs (2 READ)
- Pull Requests: list_pull_requests, get_pull_request (2 READ)
- Actions: list_workflow_runs, get_workflow_run, list_workflow_jobs, get_workflow_logs (4 READ), rerun_failed_jobs (1 WRITE)
- Deployments: list_deployments (1 READ)
- Checks: get_commit_status (1 READ)

Rate limit tracking: Every response includes _rate_limit with remaining/total/reset_at.
When budget is low (<10%), _rate_limit_warning is added.

Example:
    connector = GitHubConnector(
        connector_id="abc123",
        config={
            "organization": "my-org",
            "base_url": "https://api.github.com",
        },
        credentials={
            "token": "ghp_your_pat_here",
        },
    )

    async with connector:
        ok = await connector.test_connection()
        result = await connector.execute("list_repositories", {"type": "all"})
"""

import time
from collections.abc import Callable
from typing import Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.base import OperationDefinition, OperationResult, TypeDefinition
from meho_app.modules.connectors.github.base import GitHubHTTPBase
from meho_app.modules.connectors.github.handlers import (
    ActionsHandlerMixin,
    CommitHandlerMixin,
    DeployHandlerMixin,
    PRHandlerMixin,
    RepoHandlerMixin,
)
from meho_app.modules.connectors.github.operations import GITHUB_OPERATIONS

logger = get_logger(__name__)


class GitHubConnector(
    GitHubHTTPBase,
    RepoHandlerMixin,
    CommitHandlerMixin,
    PRHandlerMixin,
    ActionsHandlerMixin,
    DeployHandlerMixin,
):
    """
    GitHub connector using httpx for GitHub REST API access.

    Provides 12 pre-defined operations across six categories:
    - Repositories (list_repositories) -- 1 op
    - Commits (list_commits, compare_refs) -- 2 ops
    - Pull Requests (list_pull_requests, get_pull_request) -- 2 ops
    - Actions (list_workflow_runs, get_workflow_run, list_workflow_jobs, get_workflow_logs, rerun_failed_jobs) -- 5 ops
    - Deployments (list_deployments) -- 1 op
    - Checks (get_commit_status) -- 1 op

    Rate limit budget is tracked from every API response and injected
    into every operation result as _rate_limit. When remaining < 10% of
    total, a _rate_limit_warning is added to guide the agent toward
    conservative API usage.

    No topology entities via get_types -- GitHub Organization entity is
    handled via topology schema separately.
    """

    def __init__(
        self,
        connector_id: str,
        config: dict[str, Any],
        credentials: dict[str, Any],
    ):
        super().__init__(connector_id, config, credentials)

        # GitHub user info (populated on test_connection)
        self.github_user: str | None = None
        self.repo_count: int = 0

        # Build operation dispatch table from handler mixins
        self._operation_handlers: dict[str, Callable] = self._build_operation_handlers()

    # =========================================================================
    # CONNECTION & EXECUTION
    # =========================================================================

    async def test_connection(self) -> bool:
        """
        Test connection by validating auth and populating rate limit.

        1. GET /user -- confirms token is valid, stores authenticated user login
        2. GET /rate_limit -- populates initial rate limit budget
        """
        try:
            await self.connect()

            # Verify auth via /user
            user_data = await self._get("/user")
            self.github_user = user_data.get("login", "unknown")

            # Populate initial rate limit budget
            try:
                rate_data = await self._get("/rate_limit")
                core_rate = rate_data.get("resources", {}).get("core", {})
                self._rate_limit_remaining = core_rate.get("remaining")
                self._rate_limit_limit = core_rate.get("limit")
                self._rate_limit_reset = core_rate.get("reset")
            except Exception:  # noqa: S110 -- intentional silent exception handling
                # Rate limit endpoint may not be available on all GitHub instances
                pass

            logger.info(
                f"GitHub connection verified: {self.base_url} "
                f"(user: {self.github_user}, org: {self.organization}, "
                f"rate_limit: {self._rate_limit_remaining}/{self._rate_limit_limit})"
            )
            return True

        except Exception as e:
            logger.warning(f"GitHub connection test failed: {e}")
            return False

    async def execute(
        self,
        operation_id: str,
        parameters: dict[str, Any],
    ) -> OperationResult:
        """
        Execute a GitHub operation with rate limit injection.

        After handler returns result, injects _rate_limit into every response.
        If rate limit budget is low (<10%), adds _rate_limit_warning.
        """
        start_time = time.time()

        if not self._is_connected:
            await self.connect()

        handler = self._operation_handlers.get(operation_id)
        if not handler:
            return OperationResult(
                success=False,
                error=f"Unknown operation: {operation_id}",
                error_code="NOT_FOUND",
                operation_id=operation_id,
            )

        try:
            result = await handler(parameters)
            duration_ms = (time.time() - start_time) * 1000

            # Inject rate limit into every response
            if isinstance(result, dict):
                result["_rate_limit"] = self._get_rate_limit_info()
                if self._is_rate_limit_low():
                    result["_rate_limit_warning"] = (
                        f"Rate limit budget low: {self._rate_limit_remaining}/"
                        f"{self._rate_limit_limit} remaining. "
                        "Switch to conservative mode: skip non-essential calls, "
                        "target specific resources instead of listing."
                    )
            elif isinstance(result, list):
                # Wrap list in dict to attach rate limit
                result = {
                    "items": result,
                    "_rate_limit": self._get_rate_limit_info(),
                }
                if self._is_rate_limit_low():
                    result["_rate_limit_warning"] = (
                        f"Rate limit budget low: {self._rate_limit_remaining}/"
                        f"{self._rate_limit_limit} remaining. "
                        "Switch to conservative mode: skip non-essential calls, "
                        "target specific resources instead of listing."
                    )

            logger.info(f"{operation_id}: completed in {duration_ms:.1f}ms")

            return OperationResult(
                success=True,
                data=result,
                operation_id=operation_id,
                duration_ms=duration_ms,
            )
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            logger.error(f"{operation_id} failed: {e}", exc_info=True)

            error_code = self._map_http_error(e)

            return OperationResult(
                success=False,
                error=str(e),
                error_code=error_code,
                operation_id=operation_id,
                duration_ms=duration_ms,
            )

    def _build_operation_handlers(self) -> dict[str, Callable]:
        """Map operation IDs to handler methods from mixins."""
        return {
            # Repositories (1)
            "list_repositories": self._list_repositories_handler,
            # Commits (2)
            "list_commits": self._list_commits_handler,
            "compare_refs": self._compare_refs_handler,
            # Pull Requests (2)
            "list_pull_requests": self._list_pull_requests_handler,
            "get_pull_request": self._get_pull_request_handler,
            # Actions (5)
            "list_workflow_runs": self._list_workflow_runs_handler,
            "get_workflow_run": self._get_workflow_run_handler,
            "list_workflow_jobs": self._list_workflow_jobs_handler,
            "get_workflow_logs": self._get_workflow_logs_handler,
            "rerun_failed_jobs": self._rerun_failed_jobs_handler,
            # Deployments (1)
            "list_deployments": self._list_deployments_handler,
            # Checks (1)
            "get_commit_status": self._get_commit_status_handler,
        }

    def get_operations(self) -> list[OperationDefinition]:
        """Get GitHub operations for registration."""
        return list(GITHUB_OPERATIONS)

    def get_types(self) -> list[TypeDefinition]:
        """Get GitHub types for registration.

        Returns empty list -- GitHub entities are handled via topology
        schema, not connector types.
        """
        return []
