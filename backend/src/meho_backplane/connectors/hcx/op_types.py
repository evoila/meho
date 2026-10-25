# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The :class:`HcxTypedOp` metadata record and the schema helpers every HCX op module shares."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal

__all__ = [
    "GROUP_INVENTORY",
    "GROUP_JOBS_ALERTS",
    "GROUP_MANAGER",
    "GROUP_SERVICE_MESH",
    "NO_PARAMS_SCHEMA",
    "HcxTypedOp",
    "list_response_schema",
    "string_schema",
]

GROUP_MANAGER: Final = "hcx-manager"
GROUP_SERVICE_MESH: Final = "hcx-service-mesh"
GROUP_JOBS_ALERTS: Final = "hcx-jobs-alerts"
GROUP_INVENTORY: Final = "hcx-inventory"

#: Parameter schema of a read that takes no input.
NO_PARAMS_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
    "examples": [{}],
}


@dataclass(frozen=True)
class HcxTypedOp:
    """Metadata for one HCX typed op registered at lifespan startup."""

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
    """Schema of a text field that the vendor may leave empty."""
    return {"type": ["string", "null"], "description": description}


def list_response_schema(description: str, row_properties: dict[str, Any]) -> dict[str, Any]:
    """A ``{"items": [row, ...]}`` response schema."""
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": description,
                "items": {"type": "object", "properties": row_properties},
            }
        },
    }
