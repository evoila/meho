# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The operation lines of ``list_operation_groups``, behind a switch (#4713).

The tool is registered with the ``group`` input, the ``operations`` and
``operations_more`` fields of each group, the ``note`` field of the answer,
and one line in the description
(:data:`~meho_backplane.operations._group_listing_operations.GROUP_OPERATIONS_LINE`).
The reference docs list them.

While the ``GROUP_LISTING_OPERATIONS`` switch is off,
:func:`group_operations_for_settings` takes all of them out again, so
``tools/list`` stays exactly as it was.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.mcp.registry import ToolDefinition
from meho_backplane.operations._group_listing_operations import (
    GROUP_OPERATIONS_LINE,
    group_listing_operations,
)

__all__ = [
    "GROUP_INPUT_PROPERTY",
    "GROUP_NOTE_OUTPUT_PROPERTY",
    "GROUP_OPERATIONS_OUTPUT_PROPERTIES",
    "group_operations_for_settings",
]

_GROUP: Final = "group"
_NOTE: Final = "note"
_OPERATIONS: Final = "operations"
_OPERATIONS_MORE: Final = "operations_more"

#: The ``group`` input.
GROUP_INPUT_PROPERTY: Final[dict[str, Any]] = {
    _GROUP: {
        "type": ["string", "null"],
        "maxLength": 256,
        "description": "A `group_key`: list only this group.",
    },
}

#: The two fields each group gets.
GROUP_OPERATIONS_OUTPUT_PROPERTIES: Final[dict[str, Any]] = {
    _OPERATIONS: {
        "type": "array",
        "description": (
            "The group's typed and multi-step operations, by `op_id`. Call "
            "`search_operations` with an `op_id` for its full contract."
        ),
        "items": {
            "type": "object",
            "properties": {
                "op_id": {"type": "string"},
                "summary": {
                    "type": ["string", "null"],
                    "description": "First sentence of the summary, cut short.",
                },
                "safety_level": {
                    "type": "string",
                    "enum": ["safe", "caution", "dangerous", "destructive"],
                },
                "requires_approval": {"type": "boolean"},
            },
            "required": ["op_id", "summary", "safety_level", "requires_approval"],
        },
    },
    _OPERATIONS_MORE: {
        "type": "integer",
        "minimum": 1,
        "description": (
            "Operations left out of `operations` to keep the answer small. "
            "Find them with `search_operations` and `group`."
        ),
    },
}

#: The ``note`` field of the answer.
GROUP_NOTE_OUTPUT_PROPERTY: Final[dict[str, Any]] = {
    _NOTE: {
        "type": "string",
        "description": "Set when `group` names no group this listing shows.",
    },
}


def _without(schema: dict[str, Any], *names: str) -> dict[str, Any]:
    properties = {k: v for k, v in schema.get("properties", {}).items() if k not in names}
    return {**schema, "properties": properties}


def _output_without_operations(schema: dict[str, Any] | None) -> dict[str, Any] | None:
    if schema is None:
        return None
    groups = schema["properties"]["groups"]
    items = _without(groups["items"], _OPERATIONS, _OPERATIONS_MORE)
    stripped = _without(schema, _NOTE)
    stripped["properties"] = {**stripped["properties"], "groups": {**groups, "items": items}}
    return stripped


def group_operations_for_settings(definition: ToolDefinition) -> ToolDefinition:
    """Return ``list_operation_groups`` as shown under the ``GROUP_LISTING_OPERATIONS`` switch.

    Off: without ``group``, ``operations``, ``operations_more``, ``note`` and
    the description line. On: with the line at the end of the description.
    """
    description = definition.description.replace(GROUP_OPERATIONS_LINE, "")
    if group_listing_operations():
        return definition.model_copy(update={"description": description + GROUP_OPERATIONS_LINE})
    return definition.model_copy(
        update={
            "description": description,
            "inputSchema": _without(definition.inputSchema, _GROUP),
            "outputSchema": _output_without_operations(definition.outputSchema),
        }
    )
