# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Jira Connector.

Extends AtlassianHTTPConnector for Jira Cloud REST API v3 access with handler
mixins for issue search, CRUD, workflow transitions, and project listing.

8 operations across 3 categories:
- Search: search_issues, get_recent_changes, search_by_jql (3 READ/WRITE)
- Issues: get_issue, create_issue, add_comment, transition_issue (1 READ + 3 WRITE)
- Projects: list_projects (1 READ)

Example:
    connector = JiraConnector(
        connector_id="abc123",
        config={
            "base_url": "https://your-domain.atlassian.net",
        },
        credentials={
            "email": "user@example.com",
            "api_token": "your-api-token",
        },
    )

    async with connector:
        ok = await connector.test_connection()
        result = await connector.execute("search_issues", {"project": "PROJ", "status": "Open"})
"""

import time
from collections.abc import Callable
from typing import Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.atlassian.base import AtlassianHTTPConnector
from meho_app.modules.connectors.atlassian.field_resolver import FieldResolver
from meho_app.modules.connectors.base import (
    OperationDefinition,
    OperationResult,
    TypeDefinition,
)
from meho_app.modules.connectors.jira.handlers import (
    IssueHandlerMixin,
    ProjectHandlerMixin,
    SearchHandlerMixin,
)
from meho_app.modules.connectors.jira.operations import JIRA_OPERATIONS

logger = get_logger(__name__)


class JiraConnector(
    AtlassianHTTPConnector,
    SearchHandlerMixin,
    IssueHandlerMixin,
    ProjectHandlerMixin,
):
    """
    Jira Cloud connector using httpx for native Jira REST API v3 access.

    Provides 8 pre-defined operations across three categories:
    - Search (search_issues, get_recent_changes, search_by_jql) -- 3 ops
    - Issues (get_issue, create_issue, add_comment, transition_issue) -- 4 ops
    - Projects (list_projects) -- 1 op

    No topology entities -- Jira issues are not infrastructure.
    Agent reads/writes markdown; ADF conversion is invisible.
    Custom field names are human-readable, never customfield_XXXXX.
    """

    def __init__(
        self,
        connector_id: str,
        config: dict[str, Any],
        credentials: dict[str, Any],
    ) -> None:
        super().__init__(connector_id, config, credentials)

        # Field resolver for customfield_XXXXX -> human-readable names
        self._field_resolver = FieldResolver()

        # Jira user info (populated on test_connection)
        self.jira_user: str | None = None
        self.accessible_projects: int = 0

        # Build operation dispatch table from handler mixins
        self._operation_handlers: dict[str, Callable] = self._build_operation_handlers()

    # =========================================================================
    # CONNECTION & EXECUTION
    # =========================================================================

    async def test_connection(self) -> bool:
        """
        Test connection by validating auth and project access.

        1. GET /rest/api/3/myself -- confirms auth works, stores user display name
        2. GET /rest/api/3/project/search?maxResults=1 -- confirms project access
        """
        try:
            await self.connect()

            # Verify auth via /myself
            user_data = await self._get("/rest/api/3/myself")
            self.jira_user = user_data.get("displayName", user_data.get("emailAddress", "Unknown"))

            # Verify project access
            project_data = await self._get(
                "/rest/api/3/project/search",
                params={"maxResults": 1},
            )
            self.accessible_projects = project_data.get("total", 0)

            logger.info(
                f"Jira connection verified: {self.base_url} "
                f"(user: {self.jira_user}, projects: {self.accessible_projects})"
            )
            return True

        except Exception as e:
            logger.warning(f"Jira connection test failed: {e}")
            return False

    async def _execute_operation(
        self,
        operation_id: str,
        parameters: dict[str, Any],
    ) -> OperationResult:
        """Execute a Jira operation."""
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
            # Search (3)
            "search_issues": self._search_issues_handler,
            "get_recent_changes": self._get_recent_changes_handler,
            "search_by_jql": self._search_by_jql_handler,
            # Issues (4)
            "get_issue": self._get_issue_handler,
            "create_issue": self._create_issue_handler,
            "add_comment": self._add_comment_handler,
            "transition_issue": self._transition_issue_handler,
            # Projects (1)
            "list_projects": self._list_projects_handler,
        }

    def get_operations(self) -> list[OperationDefinition]:
        """Get Jira operations for registration."""
        return list(JIRA_OPERATIONS)

    def get_types(self) -> list[TypeDefinition]:
        """Get Jira types for registration.

        Returns empty list -- Jira issues are not topology entities.
        """
        return []
