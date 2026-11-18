# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The call-hint fields in the ``search_operations`` output schema (#4331).

A ranked hit carries ``required_params`` and ``target_required`` while the
``SEARCH_HIT_CALL_HINTS`` switch is on
(:mod:`~meho_backplane.operations._search_call_hints`). The registered
output schema names both fields, so the reference docs list them. With the
switch off, :func:`call_hints_schema_for_settings` takes them out of the
ranked hits again, so ``tools/list`` stays exactly as it was and an agent
reads nothing about fields that never come.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.mcp.registry import ToolDefinition
from meho_backplane.operations._search_call_hints import (
    CALL_HINT_FIELDS,
    search_hit_call_hints,
)

__all__ = [
    "CALL_HINT_PROPERTIES",
    "call_hints_schema_for_settings",
]

#: The two schema properties of a ranked hit.
CALL_HINT_PROPERTIES: Final[dict[str, Any]] = {
    "required_params": {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "Names of the parameters `call_operation` needs in `params`, in "
            "order. Names only: call `search_operations` with `op_id` for "
            "the full schema."
        ),
    },
    "target_required": {
        "type": "boolean",
        "description": (
            "True when `call_operation` needs a `target`. Pass the target's "
            "name; `list_targets` lists them."
        ),
    },
}


def _hits_without_call_hints(branch: dict[str, Any]) -> dict[str, Any]:
    """Return one ``oneOf`` branch with the call hints taken out of its hits."""
    properties = dict(branch.get("properties", {}))
    hits = properties.get("hits")
    if not (isinstance(hits, dict) and isinstance(hits.get("items"), dict)):
        return branch
    items = hits["items"]
    item_properties = {
        key: value
        for key, value in items.get("properties", {}).items()
        if key not in CALL_HINT_FIELDS
    }
    properties["hits"] = {**hits, "items": {**items, "properties": item_properties}}
    return {**branch, "properties": properties}


def call_hints_schema_for_settings(definition: ToolDefinition) -> ToolDefinition:
    """Return the tool without the call hints in its ranked-hit schema.

    Only while the ``SEARCH_HIT_CALL_HINTS`` switch is off; with it on the
    tool is returned as given.
    """
    if search_hit_call_hints() or definition.outputSchema is None:
        return definition
    schema = definition.outputSchema
    branches = [_hits_without_call_hints(branch) for branch in schema.get("oneOf", [])]
    return definition.model_copy(update={"outputSchema": {**schema, "oneOf": branches}})
