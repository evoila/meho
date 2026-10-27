# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parameter and response schemas of the Azure ``inventory`` operations.

The operation table in :mod:`meho_backplane.connectors.azure.inventory` uses
them. Every parameter field has a description and every schema has examples,
because the AI fills in the parameters from them.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.azure._subscription_set import (
    SUBSCRIPTIONS_BLOCK_SCHEMA,
    SUBSCRIPTIONS_PARAMETER,
)
from meho_backplane.connectors.azure.inventory_reads import (
    MAX_RESOURCE_ID_LENGTH,
    RESOURCE_GROUP_PATTERN,
    RESOURCE_TYPE_PATTERN,
)

__all__ = [
    "GRAPH_QUERY_PARAMS",
    "NO_PARAMS",
    "RESOURCE_LIST_PARAMS",
    "SET_PARAMS",
    "SET_RESULT_OBJECTS",
    "TAG_UPDATE_PARAMS",
    "TAG_UPDATE_RESPONSE",
    "list_response",
    "set_response",
]

_TEXT_OR_NULL: Final[list[str]] = ["string", "null"]

NO_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
    "examples": [{}],
}

SET_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {"subscriptions": SUBSCRIPTIONS_PARAMETER},
    "additionalProperties": False,
    "examples": [{}, {"subscriptions": ["azure-sub-b", "azure-sub-c"]}],
}

RESOURCE_LIST_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "maxLength": 256,
            "pattern": RESOURCE_TYPE_PATTERN,
            "description": (
                "Only resources of this type, such as Microsoft.Compute/virtualMachines "
                "or Microsoft.Network/virtualNetworks."
            ),
        },
        "resource_group": {
            "type": "string",
            "minLength": 1,
            "maxLength": 90,
            "pattern": RESOURCE_GROUP_PATTERN,
            "description": "Only resources in this resource group of the target's subscription.",
        },
    },
    "additionalProperties": False,
    "examples": [
        {},
        {"type": "Microsoft.Compute/virtualMachines"},
        {"resource_group": "rg-example", "type": "Microsoft.Storage/storageAccounts"},
    ],
}

_EXAMPLE_RESOURCE_ID: Final[str] = (
    "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-example/"
    "providers/Microsoft.Storage/storageAccounts/stexample"
)

TAG_UPDATE_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "resource_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_RESOURCE_ID_LENGTH,
            "pattern": "^/subscriptions/",
            "description": (
                "The ID of one resource group or resource in the target's own "
                "subscription, as Azure shows it (the id field of azure.resource.list)."
            ),
        },
        "mode": {
            "type": "string",
            "enum": ["merge", "remove"],
            "description": (
                "merge adds the tags or changes their values; other tags stay. remove "
                "deletes the named tags, whatever their value; other tags stay."
            ),
        },
        "tags": {
            "type": "object",
            "minProperties": 1,
            "maxProperties": 50,
            "propertyNames": {"minLength": 1, "maxLength": 512},
            "additionalProperties": {"type": "string", "maxLength": 256},
            "description": (
                "Tag name to value. For remove only the names count; give any value, "
                "for example an empty text."
            ),
        },
    },
    "required": ["resource_id", "mode", "tags"],
    "additionalProperties": False,
    "examples": [
        {"resource_id": _EXAMPLE_RESOURCE_ID, "mode": "merge", "tags": {"owner": "team-a"}},
        {"resource_id": _EXAMPLE_RESOURCE_ID, "mode": "remove", "tags": {"temp": ""}},
    ],
}

GRAPH_QUERY_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "minLength": 1,
            "maxLength": 10000,
            "description": (
                "A Resource Graph query in KQL, such as Resources | where type =~ "
                "'microsoft.compute/virtualmachines' | project name, location. Without "
                "limit, take or sample, so every page can be read."
            ),
        },
        "subscriptions": SUBSCRIPTIONS_PARAMETER,
        "page_size": {
            "type": "integer",
            "minimum": 1,
            "maximum": 1000,
            "description": (
                "Rows per page, 1 to 1,000 (Azure's default is 1,000). A smaller size "
                "only makes more pages; MEHO still reads them all, up to its limits."
            ),
        },
    },
    "required": ["query"],
    "additionalProperties": False,
    "examples": [
        {"query": "Resources | summarize count() by type"},
        {
            "query": "Resources | where type =~ 'microsoft.compute/virtualmachines' | project name",
            "subscriptions": ["azure-sub-b"],
            "page_size": 500,
        },
    ],
}


def _rows(fields: dict[str, str], description: str) -> dict[str, Any]:
    """The ``value`` list of a response: rows with the named fields."""
    return {
        "type": "array",
        "description": description,
        "items": {
            "type": "object",
            "properties": {name: {"description": text} for name, text in fields.items()},
        },
    }


def list_response(fields: dict[str, str], description: str) -> dict[str, Any]:
    """The response of a list read of the target's own subscription."""
    return {
        "type": "object",
        "properties": {
            "value": _rows(fields, description),
            "truncated": {"type": "boolean", "description": "True when MEHO stopped early."},
            "omission": {"type": _TEXT_OR_NULL, "description": "What was left out, and why."},
            "omission_kind": {"type": _TEXT_OR_NULL, "description": "The limit that stopped it."},
            "pages_read": {"type": "integer", "description": "Pages read from Azure."},
        },
        "required": ["value", "truncated", "pages_read"],
    }


def set_response(fields: dict[str, str], description: str) -> dict[str, Any]:
    """The response of a Resource Graph read over the subscription set."""
    return {
        "type": "object",
        "properties": {
            "value": _rows(fields, description),
            "complete": {
                "type": "boolean",
                "description": "True only when every requested subscription was read in full.",
            },
            "pages_read": {"type": "integer", "description": "Pages read from Azure."},
            "subscriptions": SUBSCRIPTIONS_BLOCK_SCHEMA,
            "omissions": {
                "type": "object",
                "description": "Everything left out, each with its kind and reason.",
                "properties": {
                    "count": {"type": "integer", "description": "How many omissions."},
                    "items": {"type": "array", "description": "{kind, message, ...} each."},
                },
            },
        },
        "required": ["value", "complete", "subscriptions", "omissions"],
    }


#: Keeps both blocks next to the link to the stored result when the rows go
#: behind it. ``omissions`` comes first so it gets the inline room first.
SET_RESULT_OBJECTS: Final[dict[str, Any]] = {
    "objects": {
        "omissions": ["count", "items"],
        "subscriptions": ["denied", "failed", "completed", "requested"],
    }
}


#: The response of ``azure.tag.update``.
TAG_UPDATE_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "resource_id": {"type": "string", "description": "The resource that was changed."},
        "mode": {"type": "string", "description": "merge or remove."},
        "tags": {"type": "object", "description": "All tags of the resource after it."},
        "not_present": {
            "type": "array",
            "description": "Names remove did not find, so there was nothing to remove.",
        },
    },
    "required": ["resource_id", "mode", "tags", "not_present"],
}
