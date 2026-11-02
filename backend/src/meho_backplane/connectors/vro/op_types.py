# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The :class:`VroTypedOp` metadata record, the group keys and shared schema helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal

__all__ = [
    "GROUP_EXECUTIONS",
    "GROUP_LIBRARY",
    "GROUP_SERVER",
    "GROUP_WORKFLOWS",
    "NO_PARAMS_SCHEMA",
    "VroTypedOp",
    "id_schema",
    "page_response_schema",
    "string_schema",
]

GROUP_SERVER: Final = "vro-server"
GROUP_WORKFLOWS: Final = "vro-workflows"
GROUP_EXECUTIONS: Final = "vro-executions"
GROUP_LIBRARY: Final = "vro-library"

#: Parameter schema of a read that takes no input.
NO_PARAMS_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
    "examples": [{}],
}


@dataclass(frozen=True)
class VroTypedOp:
    """Metadata for one Orchestrator typed op registered at lifespan startup."""

    op_id: str
    handler_attr: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any] | None
    group_key: str | None
    tags: tuple[str, ...]
    safety_level: Literal["safe", "caution", "dangerous", "destructive"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None


def string_schema(description: str) -> dict[str, Any]:
    """Schema of a text field that the server may leave empty."""
    return {"type": ["string", "null"], "description": description}


def id_schema(description: str) -> dict[str, Any]:
    """Schema of a required id parameter (one path segment)."""
    return {"type": "string", "minLength": 1, "maxLength": 200, "description": description}


def page_response_schema(description: str, row_properties: dict[str, Any]) -> dict[str, Any]:
    """A ``{items: [row, ...], total, start_index, count, more_rows}`` response schema."""
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": description,
                "items": {
                    "type": "object",
                    "description": "One row; field names as the server sends them.",
                    "properties": row_properties,
                },
            },
            "total": {
                "type": ["integer", "null"],
                "description": "How many objects the server has in total, when it says.",
            },
            "start_index": {
                "type": ["integer", "null"],
                "description": "Index of the first row in this page.",
            },
            "count": {"type": "integer", "description": "How many rows came back."},
            "more_rows": {
                "type": "boolean",
                "description": "True when the server has more rows than came back.",
            },
        },
    }
