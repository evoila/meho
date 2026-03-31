# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Confluence Search Handler Mixin.

Builds CQL from structured parameters so the agent never writes CQL
directly. CQL search uses v1 endpoint (/wiki/rest/api/search) because
no v2 equivalent exists. Pagination via start/limit (offset-based).
"""

from typing import Any


class SearchHandlerMixin:
    """Mixin for Confluence search operations: structured search, recent changes, raw CQL."""

    # These will be provided by ConfluenceConnector (base class / other mixins)
    async def _get(self, path: str, params: dict | None = None) -> dict: ...

    # =========================================================================
    # HANDLER METHODS
    # =========================================================================

    async def _search_pages_handler(self, params: dict[str, Any]) -> dict:
        """
        Search Confluence pages using structured filters.

        Builds CQL from the provided parameters -- the agent never writes
        CQL directly. Always includes a type clause. Joins all clauses with
        AND and appends ORDER BY lastModified DESC.
        """
        clauses: list[str] = []

        # Content type filter (default: page)
        content_type = params.get("content_type", "page")
        clauses.append(f'type = "{content_type}"')

        space_key = params.get("space_key")
        if space_key:
            clauses.append(f'space = "{space_key}"')

        title = params.get("title")
        if title:
            clauses.append(f'title ~ "{title}"')

        labels = params.get("labels")
        if labels:
            for label in labels:
                clauses.append(f'label = "{label}"')

        text = params.get("text")
        if text:
            clauses.append(f'text ~ "{text}"')

        modified_after = params.get("modified_after")
        if modified_after:
            clauses.append(f'lastModified >= "{modified_after}"')

        cql = " AND ".join(clauses)
        cql += " ORDER BY lastModified DESC"

        max_results = min(params.get("max_results", 20), 100)

        return await self._execute_cql_search(cql, max_results)

    async def _get_recent_changes_handler(self, params: dict[str, Any]) -> dict:
        """
        Get recently modified pages.

        Builds a time-windowed CQL query using now() relative time function.
        Useful for checking if runbooks or docs changed before an incident.
        """
        hours = params.get("hours", 24)
        content_type = params.get("content_type", "page")
        max_results = min(params.get("max_results", 20), 100)

        clauses: list[str] = []
        clauses.append(f'type = "{content_type}"')
        clauses.append(f'lastModified >= now("-{hours}h")')

        space_key = params.get("space_key")
        if space_key:
            clauses.append(f'space = "{space_key}"')

        cql = " AND ".join(clauses)
        cql += " ORDER BY lastModified DESC"

        return await self._execute_cql_search(cql, max_results)

    async def _search_by_cql_handler(self, params: dict[str, Any]) -> dict:
        """
        Execute a raw CQL query (escape hatch).

        Passes the CQL directly to the search endpoint without modification.
        Classified as WRITE operation so it requires agent approval.
        """
        cql = params["cql"]
        max_results = min(params.get("max_results", 20), 100)

        return await self._execute_cql_search(cql, max_results)

    # =========================================================================
    # INTERNAL HELPERS
    # =========================================================================

    async def _execute_cql_search(
        self,
        cql: str,
        max_results: int = 20,
        start: int = 0,
    ) -> dict:
        """
        Execute a CQL search via GET /wiki/rest/api/search.

        MUST use v1 endpoint -- no v2 equivalent exists for CQL search.
        Pagination is offset-based (start + limit), not cursor-based.
        """
        data = await self._get(
            "/wiki/rest/api/search",
            params={
                "cql": cql,
                "limit": max_results,
                "start": start,
            },
        )

        results = data.get("results", [])
        total_size = data.get("totalSize", 0)

        pages = []
        for result in results:
            content = result.get("content", {})
            space = content.get("space", {})

            pages.append(
                {
                    "id": content.get("id"),
                    "title": content.get("title"),
                    "space_key": space.get("key", ""),
                    "space_name": space.get("name", ""),
                    "last_modified": result.get("lastModified"),
                    "excerpt": result.get("excerpt", ""),
                }
            )

        search_result: dict[str, Any] = {
            "total": total_size,
            "pages": pages,
        }

        if start + max_results < total_size:
            search_result["next_start"] = start + max_results

        return search_result
