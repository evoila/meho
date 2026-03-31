# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Confluence Content Handler Mixin.

Handles page CRUD: get details, create, update, add footer comment.
ADF conversion is transparent -- the agent reads/writes markdown,
never sees ADF. All page operations use the v2 API (/wiki/api/v2/pages).

CRITICAL: body.value for all write operations MUST be json.dumps(adf_doc),
NOT the dict itself. The v2 API requires ADF as a JSON string.
"""

import json
from typing import Any

import httpx

from meho_app.modules.connectors.atlassian.adf_converter import (
    adf_to_markdown,
    markdown_to_adf,
)


class ContentHandlerMixin:
    """Mixin for Confluence content operations: get, create, update, comment."""

    # These will be provided by ConfluenceConnector (base class / other mixins)
    async def _get(self, path: str, params: dict | None = None) -> dict: ...

    async def _post(self, path: str, json: Any = None) -> dict: ...

    async def _put(self, path: str, json: Any = None) -> dict: ...

    # =========================================================================
    # HANDLER METHODS
    # =========================================================================

    async def _get_page_handler(self, params: dict[str, Any]) -> dict:
        """
        Get full page content with metadata.

        Uses v2 API with body-format=atlas_doc_format. Parses ADF body
        via json.loads then converts to markdown via adf_to_markdown().
        Also fetches child pages as references.
        """
        page_id = params["page_id"]

        # Get page with ADF body, labels, and version info
        data = await self._get(
            f"/wiki/api/v2/pages/{page_id}",
            params={
                "body-format": "atlas_doc_format",
                "include-labels": "true",
                "include-version": "true",
            },
        )

        # Parse ADF body from JSON string
        body_adf_str = data.get("body", {}).get("atlas_doc_format", {}).get("value", "")
        adf_doc = json.loads(body_adf_str) if body_adf_str else None
        content_md = adf_to_markdown(adf_doc)

        # Extract labels
        labels_data = data.get("labels", {})
        labels: list[str] = []
        if isinstance(labels_data, dict):
            labels = [label.get("name", "") for label in labels_data.get("results", [])]
        elif isinstance(labels_data, list):
            labels = [label.get("name", "") for label in labels_data]

        # Extract version info
        version_data = data.get("version", {})

        # Build page URL
        space_id = data.get("spaceId")
        page_url = f"{self.base_url}/wiki/pages/{page_id}"

        result: dict[str, Any] = {
            "id": data.get("id"),
            "title": data.get("title"),
            "space_id": space_id,
            "version": version_data.get("number"),
            "last_modified_by": version_data.get("authorId"),
            "content": content_md,
            "labels": labels,
            "url": page_url,
        }

        # Fetch child pages as references
        try:
            children_data = await self._get(
                f"/wiki/api/v2/pages/{page_id}/children",
            )
            children = children_data.get("results", [])
            result["children"] = [
                {"id": child.get("id"), "title": child.get("title")} for child in children
            ]
        except Exception:
            result["children"] = []

        return result

    async def _create_page_handler(self, params: dict[str, Any]) -> dict:
        """
        Create a new Confluence page.

        Resolves space_key to spaceId via the v2 spaces endpoint.
        Converts markdown to ADF. CRITICAL: body.value MUST be
        json.dumps(adf_doc), NOT the dict itself.
        """
        space_key = params["space_key"]
        title = params["title"]
        content_md = params["content"]
        parent_page_id = params.get("parent_page_id")

        # Resolve space_key to numeric spaceId
        space_data = await self._get(
            "/wiki/api/v2/spaces",
            params={"keys": space_key},
        )
        spaces = space_data.get("results", [])
        if not spaces:
            raise ValueError(f"Space with key '{space_key}' not found")
        space_id = spaces[0].get("id")

        # Convert markdown to ADF
        adf_doc = markdown_to_adf(content_md)

        # Build payload
        payload: dict[str, Any] = {
            "spaceId": space_id,
            "status": "current",
            "title": title,
            "body": {
                "representation": "atlas_doc_format",
                "value": json.dumps(adf_doc),  # STRING, not dict
            },
        }

        if parent_page_id:
            payload["parentId"] = parent_page_id

        data = await self._post("/wiki/api/v2/pages", json=payload)

        return {
            "id": data.get("id"),
            "title": data.get("title"),
            "space_id": space_id,
            "url": f"{self.base_url}/wiki/pages/{data.get('id', '')}",
            "message": f"Page '{title}' created successfully",
        }

    async def _update_page_handler(self, params: dict[str, Any]) -> dict:
        """
        Update page content with transparent version handling.

        Fetches current page to extract version.number and current title.
        Increments version by 1. On 409 Conflict: refetches current page,
        re-increments version, retries PUT once. If second attempt also
        fails, raises.
        """
        page_id = params["page_id"]
        new_content_md = params["content"]
        new_title = params.get("title")

        # Fetch current page to get version number and current title
        current = await self._get(
            f"/wiki/api/v2/pages/{page_id}",
            params={"body-format": "atlas_doc_format"},
        )
        current_version = current.get("version", {}).get("number", 1)
        current_title = current.get("title", "")

        # Convert markdown to ADF
        adf_doc = markdown_to_adf(new_content_md)

        payload = {
            "id": str(page_id),
            "status": "current",
            "title": new_title or current_title,
            "body": {
                "representation": "atlas_doc_format",
                "value": json.dumps(adf_doc),
            },
            "version": {
                "number": current_version + 1,
                "message": "Updated by MEHO",
            },
        }

        try:
            data = await self._put(f"/wiki/api/v2/pages/{page_id}", json=payload)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 409:
                # Version conflict -- refetch and retry once
                current = await self._get(
                    f"/wiki/api/v2/pages/{page_id}",
                    params={"body-format": "atlas_doc_format"},
                )
                new_version = current.get("version", {}).get("number", 1) + 1
                payload["version"]["number"] = new_version
                data = await self._put(f"/wiki/api/v2/pages/{page_id}", json=payload)
            else:
                raise

        return {
            "id": data.get("id"),
            "title": data.get("title"),
            "version": data.get("version", {}).get("number"),
            "url": f"{self.base_url}/wiki/pages/{page_id}",
            "message": f"Page '{data.get('title', '')}' updated successfully",
        }

    async def _add_comment_handler(self, params: dict[str, Any]) -> dict:
        """
        Add a footer comment to a Confluence page.

        Converts markdown body to ADF. Uses v2 footer-comments endpoint.
        CRITICAL: body.value MUST be json.dumps(adf_doc), NOT the dict.
        """
        page_id = params["page_id"]
        body_md = params["body"]

        # Convert markdown to ADF
        adf_doc = markdown_to_adf(body_md)

        payload = {
            "pageId": str(page_id),
            "body": {
                "representation": "atlas_doc_format",
                "value": json.dumps(adf_doc),
            },
        }

        data = await self._post("/wiki/api/v2/footer-comments", json=payload)

        return {
            "id": data.get("id"),
            "page_id": page_id,
            "message": f"Comment added to page {page_id}",
        }
