# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Confluence Connector.

Extends AtlassianHTTPConnector for Confluence Cloud v2 REST API access with
handler mixins for page search, CRUD, comments, and space listing.

8 operations across 3 categories:
- Search: search_pages, get_recent_changes, search_by_cql (2 READ + 1 WRITE)
- Content: get_page, create_page, update_page, add_comment (1 READ + 3 WRITE)
- Spaces: list_spaces (1 READ)

Example:
    connector = ConfluenceConnector(
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
        result = await connector.execute("search_pages", {"space_key": "OPS", "text": "runbook"})
"""

import time
from collections.abc import Callable
from typing import Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.atlassian.base import AtlassianHTTPConnector
from meho_app.modules.connectors.base import OperationDefinition, OperationResult, TypeDefinition
from meho_app.modules.connectors.confluence.handlers import (
    ContentHandlerMixin,
    SearchHandlerMixin,
    SpaceHandlerMixin,
)
from meho_app.modules.connectors.confluence.operations import CONFLUENCE_OPERATIONS

logger = get_logger(__name__)


class ConfluenceConnector(
    AtlassianHTTPConnector,
    SearchHandlerMixin,
    ContentHandlerMixin,
    SpaceHandlerMixin,
):
    """
    Confluence Cloud connector using httpx for native Confluence REST API v2 access.

    Provides 8 pre-defined operations across three categories:
    - Search (search_pages, get_recent_changes, search_by_cql) -- 3 ops
    - Content (get_page, create_page, update_page, add_comment) -- 4 ops
    - Spaces (list_spaces) -- 1 op

    No topology entities -- Confluence pages are not infrastructure.
    Agent reads/writes markdown; ADF conversion is invisible.
    CQL search uses v1 API; page CRUD uses v2 API.
    """

    def __init__(
        self,
        connector_id: str,
        config: dict[str, Any],
        credentials: dict[str, Any],
    ):
        super().__init__(connector_id, config, credentials)

        # Confluence user info (populated on test_connection)
        self.confluence_user: str | None = None
        self.accessible_spaces: int = 0

        # Build operation dispatch table from handler mixins
        self._operation_handlers: dict[str, Callable] = self._build_operation_handlers()

    # =========================================================================
    # CONNECTION & EXECUTION
    # =========================================================================

    async def test_connection(self) -> bool:
        """
        Test connection by validating auth and space access.

        GET /wiki/api/v2/spaces?limit=1 -- confirms auth works and
        the user has access to at least one space.
        """
        try:
            await self.connect()

            # Verify auth and access via spaces endpoint
            space_data = await self._get(
                "/wiki/api/v2/spaces",
                params={"limit": 1},
            )

            results = space_data.get("results", [])
            self.accessible_spaces = len(results)

            logger.info(
                f"Confluence connection verified: {self.base_url} "
                f"(spaces accessible: {self.accessible_spaces})"
            )
            return True

        except Exception as e:
            logger.warning(f"Confluence connection test failed: {e}")
            return False

    async def execute(
        self,
        operation_id: str,
        parameters: dict[str, Any],
    ) -> OperationResult:
        """Execute a Confluence operation."""
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
            "search_pages": self._search_pages_handler,
            "get_recent_changes": self._get_recent_changes_handler,
            "search_by_cql": self._search_by_cql_handler,
            # Content (4)
            "get_page": self._get_page_handler,
            "create_page": self._create_page_handler,
            "update_page": self._update_page_handler,
            "add_comment": self._add_comment_handler,
            # Spaces (1)
            "list_spaces": self._list_spaces_handler,
        }

    def get_operations(self) -> list[OperationDefinition]:
        """Get Confluence operations for registration."""
        return list(CONFLUENCE_OPERATIONS)

    def get_types(self) -> list[TypeDefinition]:
        """Get Confluence types for registration.

        Returns empty list -- Confluence pages are not topology entities.
        """
        return []
