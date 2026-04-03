# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Jira Custom Field Resolver.

Maps customfield_XXXXX IDs to human-readable names and back.
Lazy-loaded from the Jira REST API on first use, cached for
the lifetime of the connector instance.

The agent sees "Story Points: 5" instead of "customfield_10016: 5".
Write operations reverse-map human names back to customfield IDs.
"""

from typing import Any

import httpx

from meho_app.core.otel import get_logger

logger = get_logger(__name__)

# Set of complex field value keys we know how to extract
_EXTRACTABLE_KEYS = {"value", "displayName", "name", "emailAddress"}

# Track logged unknown types to avoid log spam
_logged_unknown_types: set = set()


class FieldResolver:
    """
    Resolves Jira custom field IDs to human-readable names.

    Fetches field metadata from /rest/api/3/field on first use,
    then caches the mapping. Strips null/empty field values and
    flattens complex objects to their most useful string representation.
    """

    def __init__(self) -> None:
        self._field_map: dict[str, str] = {}  # customfield_XXXXX -> "Human Name"
        self._reverse_map: dict[str, str] = {}  # "human name" (lowercase) -> "customfield_XXXXX"
        self._loaded: bool = False

    async def load(self, client: httpx.AsyncClient) -> None:
        """
        Fetch field metadata from Jira REST API.

        GET /rest/api/3/field returns all fields (system + custom).
        We only cache custom fields (id starts with "customfield_").
        Only loads once -- subsequent calls are no-ops.

        Args:
            client: Authenticated httpx.AsyncClient for the Jira instance
        """
        if self._loaded:
            return

        try:
            response = await client.get("/rest/api/3/field")
            response.raise_for_status()
            fields = response.json()

            for field in fields:
                field_id = field.get("id", "")
                field_name = field.get("name", "")
                if field_id.startswith("customfield_") and field_name:
                    self._field_map[field_id] = field_name
                    self._reverse_map[field_name.lower()] = field_id

            self._loaded = True
            logger.info(
                f"Field resolver loaded {len(self._field_map)} custom fields",
            )
        except Exception as e:
            logger.warning(f"Failed to load field metadata: {e}")
            # Don't set _loaded -- allow retry on next call

    def resolve_fields(self, issue_fields: dict[str, Any]) -> dict[str, Any]:
        """
        Replace custom field IDs with human-readable names.

        - Strips entries where value is None, empty string, or empty list
        - For complex field values (dicts with value/displayName/name/emailAddress),
          extracts the most useful string representation
        - For arrays of objects, extracts value/name from each
        - Unknown complex types fall back to str(value)

        Args:
            issue_fields: Raw Jira issue fields dict

        Returns:
            Cleaned dict with human-readable field names
        """
        resolved: dict[str, Any] = {}

        for field_id, value in issue_fields.items():
            # Strip empty values
            if value is None:
                continue
            if value == "":
                continue
            if isinstance(value, list) and len(value) == 0:
                continue

            # Resolve field name
            field_name = self._field_map.get(field_id, field_id)

            # Flatten complex values
            resolved[field_name] = self._flatten_value(value)

        return resolved

    def reverse_resolve(self, field_name: str) -> str:
        """
        Map human-readable field name back to customfield_XXXXX.

        Case-insensitive lookup. Returns the original name if
        no mapping is found (handles system fields like 'summary').

        Args:
            field_name: Human-readable field name (e.g., "Story Points")

        Returns:
            Custom field ID (e.g., "customfield_10016") or original name
        """
        return self._reverse_map.get(field_name.lower(), field_name)

    def _flatten_value(self, value: Any) -> Any:
        """
        Flatten complex Jira field values to their most useful representation.

        Jira fields can be:
        - Simple values (str, int, float, bool) -- pass through
        - Dicts with 'value', 'displayName', 'name', or 'emailAddress' -- extract
        - Lists of dicts -- extract from each element
        - Other complex types -- str() fallback
        """
        if isinstance(value, (str, int, float, bool)):
            return value

        if isinstance(value, dict):
            return self._extract_from_dict(value)

        if isinstance(value, list):
            return self._extract_from_list(value)

        return str(value)

    def _extract_from_dict(self, d: dict[str, Any]) -> Any:
        """Extract the most useful string from a complex field dict."""
        # Try known extractable keys in priority order
        for key in ("displayName", "name", "value", "emailAddress"):
            if d.get(key):
                return d[key]

        # Unknown dict structure -- log first encounter, then str() fallback
        type_key = str(sorted(d.keys()))
        if type_key not in _logged_unknown_types:
            _logged_unknown_types.add(type_key)
            logger.debug(
                f"Unrecognized field value structure: keys={list(d.keys())}",
            )

        return str(d)

    def _extract_from_list(self, items: list) -> Any:
        """Extract values from a list of objects."""
        if not items:
            return []

        # If items are simple values, return as-is
        if isinstance(items[0], (str, int, float, bool)):
            return items

        # If items are dicts, extract from each
        if isinstance(items[0], dict):
            extracted = []
            for item in items:
                if isinstance(item, dict):
                    extracted.append(self._extract_from_dict(item))
                else:
                    extracted.append(str(item))
            return extracted

        return [str(item) for item in items]
