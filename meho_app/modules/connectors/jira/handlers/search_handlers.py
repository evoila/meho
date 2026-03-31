# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Jira Search Handler Mixin.

Builds JQL from structured parameters so the agent never writes JQL
directly. Handles pagination via nextPageToken (not startAt).
Uses POST /rest/api/3/search/jql (not the deprecated GET /rest/api/3/search).
"""

from typing import Any

from meho_app.modules.connectors.atlassian.adf_converter import adf_to_markdown


class SearchHandlerMixin:
    """Mixin for Jira search operations: structured search, recent changes, raw JQL."""

    # These will be provided by JiraConnector (base class / other mixins)
    async def _post(self, path: str, json: Any = None) -> dict: ...

    # =========================================================================
    # HANDLER METHODS
    # =========================================================================

    async def _search_issues_handler(self, params: dict[str, Any]) -> dict:
        """
        Search Jira issues using structured filters.

        Builds JQL from the provided parameters -- the agent never writes
        JQL directly. Joins all clauses with AND and appends ORDER BY updated DESC.
        """
        clauses: list[str] = []

        project = params.get("project")
        if project:
            clauses.append(f'project = "{project}"')

        status = params.get("status")
        if status:
            clauses.append(f'status = "{status}"')

        issue_type = params.get("type")
        if issue_type:
            clauses.append(f'issuetype = "{issue_type}"')

        assignee = params.get("assignee")
        if assignee:
            if assignee == "currentUser()":
                clauses.append("assignee = currentUser()")
            else:
                clauses.append(f'assignee = "{assignee}"')

        priority = params.get("priority")
        if priority:
            clauses.append(f'priority = "{priority}"')

        labels = params.get("labels")
        if labels:
            for label in labels:
                clauses.append(f'labels = "{label}"')

        text = params.get("text")
        if text:
            clauses.append(f'text ~ "{text}"')

        updated_after = params.get("updated_after")
        if updated_after:
            clauses.append(f'updated >= "{updated_after}"')

        created_after = params.get("created_after")
        if created_after:
            clauses.append(f'created >= "{created_after}"')

        jql = " AND ".join(clauses) if clauses else "ORDER BY updated DESC"
        if clauses:
            jql += " ORDER BY updated DESC"

        max_results = min(params.get("max_results", 20), 100)
        next_page_token = params.get("next_page_token")

        return await self._execute_jql_search(jql, max_results, next_page_token)

    async def _get_recent_changes_handler(self, params: dict[str, Any]) -> dict:
        """
        Get issues recently created or updated in a project.

        Builds a time-windowed JQL query using negative relative time.
        """
        project = params["project"]
        hours = params.get("hours", 24)
        max_results = min(params.get("max_results", 20), 100)

        jql = f'project = "{project}" AND updated >= "-{hours}h" ORDER BY updated DESC'
        return await self._execute_jql_search(jql, max_results)

    async def _search_by_jql_handler(self, params: dict[str, Any]) -> dict:
        """
        Execute a raw JQL query (escape hatch).

        Passes the JQL directly to the search endpoint without modification.
        Classified as WRITE operation so it requires agent approval.
        """
        jql = params["jql"]
        max_results = min(params.get("max_results", 20), 100)
        next_page_token = params.get("next_page_token")

        return await self._execute_jql_search(jql, max_results, next_page_token)

    # =========================================================================
    # INTERNAL HELPERS
    # =========================================================================

    async def _execute_jql_search(
        self,
        jql: str,
        max_results: int = 20,
        next_page_token: str | None = None,
    ) -> dict:
        """
        Execute a JQL search via POST /rest/api/3/search/jql.

        Uses the v3 search endpoint with POST body (not the deprecated
        GET /rest/api/3/search). Pagination via nextPageToken.
        """
        body: dict[str, Any] = {
            "jql": jql,
            "fields": [
                "summary",
                "status",
                "assignee",
                "priority",
                "issuetype",
                "created",
                "updated",
                "labels",
                "reporter",
                "resolution",
                "description",
            ],
            "maxResults": max_results,
        }

        if next_page_token:
            body["nextPageToken"] = next_page_token

        data = await self._post("/rest/api/3/search/jql", json=body)
        return await self._serialize_search_results(data)

    async def _serialize_search_results(self, data: dict) -> dict:
        """
        Serialize Jira search results for agent consumption.

        Resolves custom fields, converts ADF descriptions to markdown
        summaries (first 200 chars), and extracts pagination token.
        """
        # Lazy-load field resolver
        await self._ensure_field_resolver()

        total = data.get("total", 0)
        issues_data = data.get("issues", [])
        token = data.get("nextPageToken")

        issues = []
        for issue in issues_data:
            fields = issue.get("fields", {})

            # Resolve custom fields to human-readable names
            resolved = self._field_resolver.resolve_fields(fields)

            # Convert ADF description to markdown preview
            description_adf = fields.get("description")
            description_md = adf_to_markdown(description_adf)
            description_preview = (
                description_md[:200] + "..." if len(description_md) > 200 else description_md
            )

            # Extract key fields
            status_obj = fields.get("status", {})
            assignee_obj = fields.get("assignee")
            priority_obj = fields.get("priority")
            issue_type_obj = fields.get("issuetype", {})
            reporter_obj = fields.get("reporter")
            resolution_obj = fields.get("resolution")

            issue_result = {
                "key": issue.get("key"),
                "summary": fields.get("summary", ""),
                "status": status_obj.get("name", "") if status_obj else "",
                "assignee": assignee_obj.get("displayName", "Unassigned")
                if assignee_obj
                else "Unassigned",
                "priority": priority_obj.get("name", "") if priority_obj else "",
                "type": issue_type_obj.get("name", "") if issue_type_obj else "",
                "labels": fields.get("labels", []),
                "reporter": reporter_obj.get("displayName", "") if reporter_obj else "",
                "resolution": resolution_obj.get("name", "") if resolution_obj else None,
                "created": fields.get("created", ""),
                "updated": fields.get("updated", ""),
                "description_preview": description_preview,
            }

            # Add any resolved custom fields (exclude system fields already extracted)
            system_fields = {
                "summary",
                "status",
                "assignee",
                "priority",
                "issuetype",
                "created",
                "updated",
                "labels",
                "reporter",
                "resolution",
                "description",
            }
            for field_name, field_value in resolved.items():
                if field_name not in system_fields:
                    issue_result[field_name] = field_value

            issues.append(issue_result)

        result: dict[str, Any] = {
            "total": total,
            "issues": issues,
        }
        if token:
            result["next_page_token"] = token

        return result

    async def _ensure_field_resolver(self) -> None:
        """Lazy-load field resolver on first use."""
        if not self._field_resolver._loaded:
            if not self._client:
                await self.connect()
            await self._field_resolver.load(self._client)
