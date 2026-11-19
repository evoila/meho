# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``include_raw`` input of the two discovery tools, behind a switch (#4712).

``search_operations`` and ``list_operation_groups`` are registered with the
``include_raw`` input, the ``raw_operations`` output field and one line in
the description that names the flag
(:data:`~meho_backplane.operations._search_typed_first.INCLUDE_RAW_LINE`).
The reference docs list them.

While the ``SEARCH_TYPED_FIRST`` switch is off,
:func:`include_raw_for_settings` takes all three out again, so
``tools/list`` stays exactly as it was. With it on, the line is added back
when a short tool text replaced the description.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.mcp.registry import ToolDefinition
from meho_backplane.operations._search_typed_first import INCLUDE_RAW_LINE, search_typed_first

__all__ = [
    "GROUPS_RAW_OUTPUT_PROPERTY",
    "INCLUDE_RAW_INPUT_PROPERTY",
    "SEARCH_RAW_OUTPUT_PROPERTY",
    "include_raw_for_settings",
]

_INCLUDE_RAW: Final = "include_raw"
_RAW_OPERATIONS: Final = "raw_operations"

#: The input property, the same on both tools.
INCLUDE_RAW_INPUT_PROPERTY: Final[dict[str, Any]] = {
    _INCLUDE_RAW: {
        "type": "boolean",
        "default": False,
        "description": (
            "Also show raw operations made from the vendor API description. "
            "Default: only typed and multi-step operations."
        ),
    },
}


def _raw_output(count_field: str, count_text: str) -> dict[str, Any]:
    return {
        _RAW_OPERATIONS: {
            "type": "object",
            "description": "Whether raw operations are shown, and why.",
            "properties": {
                "included": {"type": "boolean"},
                count_field: {"type": "integer", "minimum": 0, "description": count_text},
                "note": {"type": ["string", "null"]},
            },
            "required": ["included", count_field, "note"],
        },
    }


#: The ``raw_operations`` output field of a ranked search.
SEARCH_RAW_OUTPUT_PROPERTY: Final[dict[str, Any]] = _raw_output(
    "hidden_matches", "Raw operations left out that would also be hits."
)

#: The ``raw_operations`` output field of a group listing.
GROUPS_RAW_OUTPUT_PROPERTY: Final[dict[str, Any]] = _raw_output(
    "hidden_groups", "Groups left out because they hold only raw operations."
)


def _without(schema: dict[str, Any], name: str) -> dict[str, Any]:
    properties = {k: v for k, v in schema.get("properties", {}).items() if k != name}
    return {**schema, "properties": properties}


def _output_without_raw(schema: dict[str, Any] | None) -> dict[str, Any] | None:
    if schema is None:
        return None
    if "oneOf" in schema:
        branches = [_without(branch, _RAW_OPERATIONS) for branch in schema["oneOf"]]
        return {**schema, "oneOf": branches}
    return _without(schema, _RAW_OPERATIONS)


def include_raw_for_settings(definition: ToolDefinition) -> ToolDefinition:
    """Return the tool as shown under the ``SEARCH_TYPED_FIRST`` switch.

    Off: without ``include_raw``, ``raw_operations`` and the description
    line. On: with the line at the end of the description.
    """
    description = definition.description.replace(INCLUDE_RAW_LINE, "")
    if search_typed_first():
        return definition.model_copy(update={"description": description + INCLUDE_RAW_LINE})
    return definition.model_copy(
        update={
            "description": description,
            "inputSchema": _without(definition.inputSchema, _INCLUDE_RAW),
            "outputSchema": _output_without_raw(definition.outputSchema),
        }
    )
