# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Jira Issue Handler Mixin.

Handles issue CRUD: get details, create, add comment, and workflow
transitions. ADF conversion is transparent -- the agent reads/writes
markdown, never sees ADF.
"""

from typing import Any

from meho_app.modules.connectors.atlassian.adf_converter import (
    adf_to_markdown,
    markdown_to_adf,
)


class IssueHandlerMixin:
    """Mixin for Jira issue operations: get, create, comment, transition."""

    # These will be provided by JiraConnector (base class / other mixins)
    async def _get(self, path: str, params: dict | None = None) -> dict: ...  # type: ignore[empty-body]

    async def _post(self, path: str, json: Any = None) -> dict: ...  # type: ignore[empty-body]

    async def _ensure_field_resolver(self) -> None: ...

    _field_resolver: Any
    base_url: str

    # =========================================================================
    # HANDLER METHODS
    # =========================================================================

    async def _get_issue_handler(self, params: dict[str, Any]) -> dict:
        """
        Get full details of a Jira issue including comments.

        Converts description ADF to markdown, fetches and converts all
        comment bodies to markdown, resolves custom field names to
        human-readable text.
        """
        issue_key = params["issue_key"]

        # Lazy-load field resolver
        await self._ensure_field_resolver()

        # Get issue with rendered fields for fallback
        issue_data = await self._get(
            f"/rest/api/3/issue/{issue_key}",
            params={"expand": "renderedFields"},
        )

        fields = issue_data.get("fields", {})

        # Convert description ADF to markdown
        description_adf = fields.get("description")
        description_md = adf_to_markdown(description_adf)

        # Resolve custom fields
        resolved = self._field_resolver.resolve_fields(fields)

        # Extract key fields
        status_obj = fields.get("status", {})
        assignee_obj = fields.get("assignee")
        priority_obj = fields.get("priority")
        issue_type_obj = fields.get("issuetype", {})
        reporter_obj = fields.get("reporter")
        resolution_obj = fields.get("resolution")
        creator_obj = fields.get("creator")

        result: dict[str, Any] = {
            "key": issue_data.get("key"),
            "summary": fields.get("summary", ""),
            "status": status_obj.get("name", "") if status_obj else "",
            "type": issue_type_obj.get("name", "") if issue_type_obj else "",
            "priority": priority_obj.get("name", "") if priority_obj else "",
            "assignee": assignee_obj.get("displayName", "Unassigned")
            if assignee_obj
            else "Unassigned",
            "reporter": reporter_obj.get("displayName", "") if reporter_obj else "",
            "creator": creator_obj.get("displayName", "") if creator_obj else "",
            "resolution": resolution_obj.get("name", "") if resolution_obj else None,
            "labels": fields.get("labels", []),
            "created": fields.get("created", ""),
            "updated": fields.get("updated", ""),
            "description": description_md,
            "url": f"{self.base_url}/browse/{issue_data.get('key', '')}",
        }

        # Add resolved custom fields (exclude system fields already extracted)
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
            "creator",
        }
        for field_name, field_value in resolved.items():
            if field_name not in system_fields:
                result[field_name] = field_value

        # Fetch and convert comments
        comments_data = await self._get(f"/rest/api/3/issue/{issue_key}/comment")
        comments_list = comments_data.get("comments", [])

        comments = []
        for comment in comments_list:
            body_adf = comment.get("body")
            body_md = adf_to_markdown(body_adf)
            author_obj = comment.get("author", {})

            comments.append(
                {
                    "id": comment.get("id"),
                    "author": author_obj.get("displayName", "Unknown"),
                    "body": body_md,
                    "created": comment.get("created", ""),
                    "updated": comment.get("updated", ""),
                }
            )

        result["comments"] = comments
        result["comment_count"] = len(comments)

        return result

    async def _create_issue_handler(self, params: dict[str, Any]) -> dict:
        """
        Create a new Jira issue with markdown description.

        Converts markdown description to ADF for the Jira API.
        Reverse-resolves any human-readable custom field names back
        to customfield_XXXXX IDs.
        """
        await self._ensure_field_resolver()

        # Build issue fields
        issue_fields: dict[str, Any] = {
            "project": {"key": params["project"]},
            "issuetype": {"name": params["issue_type"]},
            "summary": params["summary"],
        }

        # Convert markdown description to ADF
        description = params.get("description")
        if description:
            issue_fields["description"] = markdown_to_adf(description)

        # Priority
        priority = params.get("priority")
        if priority:
            issue_fields["priority"] = {"name": priority}

        # Labels
        labels = params.get("labels")
        if labels:
            issue_fields["labels"] = labels

        # Assignee
        assignee = params.get("assignee")
        if assignee:
            issue_fields["assignee"] = {"id": assignee}

        payload = {"fields": issue_fields}

        data = await self._post("/rest/api/3/issue", json=payload)

        return {
            "key": data.get("key"),
            "id": data.get("id"),
            "url": f"{self.base_url}/browse/{data.get('key', '')}",
            "message": f"Issue {data.get('key')} created successfully",
        }

    async def _add_comment_handler(self, params: dict[str, Any]) -> dict:
        """
        Add a comment to a Jira issue.

        Converts markdown body to ADF for the Jira API.
        """
        issue_key = params["issue_key"]
        body_md = params["body"]

        # Convert markdown to ADF
        body_adf = markdown_to_adf(body_md)

        payload = {"body": body_adf}

        data = await self._post(
            f"/rest/api/3/issue/{issue_key}/comment",
            json=payload,
        )

        return {
            "id": data.get("id"),
            "issue_key": issue_key,
            "message": f"Comment added to {issue_key}",
        }

    async def _transition_issue_handler(self, params: dict[str, Any]) -> dict:
        """
        Change the status of a Jira issue through its workflow.

        First fetches available transitions, then finds the transition
        matching the target status name (case-insensitive). If no match,
        returns the list of available transitions for the agent to choose.
        """
        issue_key = params["issue_key"]
        target_status = params["target_status"]

        # Get available transitions
        transitions_data = await self._get(f"/rest/api/3/issue/{issue_key}/transitions")
        transitions = transitions_data.get("transitions", [])

        # Find matching transition (case-insensitive)
        matching_transition = None
        for transition in transitions:
            to_status = transition.get("to", {}).get("name", "")
            if to_status.lower() == target_status.lower():
                matching_transition = transition
                break

        if not matching_transition:
            available = [
                {
                    "name": t.get("name", ""),
                    "to_status": t.get("to", {}).get("name", ""),
                }
                for t in transitions
            ]
            return {
                "error": f"No transition to '{target_status}' available for {issue_key}",
                "available_transitions": available,
                "hint": "Use one of the available transition target statuses listed above.",
            }

        # Perform the transition
        transition_id = matching_transition.get("id")
        payload = {"transition": {"id": transition_id}}

        try:
            await self._post(
                f"/rest/api/3/issue/{issue_key}/transitions",
                json=payload,
            )
        except Exception as e:
            # Handle 409 Conflict as retriable
            error_str = str(e)
            if "409" in error_str:
                return {
                    "error": f"Conflict transitioning {issue_key} to '{target_status}' -- "
                    "the issue may have been modified concurrently. Try again.",
                    "retriable": True,
                }
            raise

        return {
            "issue_key": issue_key,
            "from_status": None,  # Jira transition response doesn't include previous status
            "to_status": matching_transition.get("to", {}).get("name", ""),
            "transition_name": matching_transition.get("name", ""),
            "message": f"Issue {issue_key} transitioned to '{matching_transition.get('to', {}).get('name', '')}'",
        }
