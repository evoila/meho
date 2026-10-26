# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared pieces of the Salt typed-op metadata: the op dataclass, groups, enums, schemas."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "GROUP_JOBS",
    "GROUP_MINIONS",
    "GROUP_SERVER",
    "JOB_STATES",
    "KEY_ROW_PROPERTIES",
    "KEY_STATES",
    "KEY_STATE_SCHEMA",
    "MASTER_ID_SCHEMA",
    "MINION_ID_SCHEMA",
    "PAGE_SCHEMA",
    "TAGS",
    "SaltTypedOp",
]

GROUP_SERVER = "salt-server"
GROUP_MINIONS = "salt-minions"
GROUP_JOBS = "salt-jobs"

#: Minion key states, as the ``minions.get_minion_key_state`` page lists them.
KEY_STATES: tuple[str, ...] = ("accepted", "pending", "rejected", "denied")

#: Job states, as the ``cmd.get_cmds`` page lists them.
JOB_STATES: tuple[str, ...] = (
    "new",
    "retrieved",
    "skipped",
    "pausing",
    "paused",
    "resuming",
    "resumed",
    "completed_missing_returns",
    "completed_failures",
    "completed_all_successful",
    "stopping",
    "stopped",
    "disabled",
)


@dataclass(frozen=True)
class SaltTypedOp:
    """Metadata for one Salt typed op registered at lifespan startup."""

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


#: Tags every Salt read carries.
TAGS = ("read-only", "salt", "aria-automation-config")


def _text(description: str) -> dict[str, Any]:
    return {"type": ["string", "null"], "description": description}


def _limit(default: int, maximum: int) -> dict[str, Any]:
    return {
        "type": "integer",
        "minimum": 1,
        "maximum": maximum,
        "default": default,
        "description": f"Rows per page, 1 to {maximum}. Default {default}.",
    }


PAGE_SCHEMA: dict[str, Any] = {
    "type": "integer",
    "minimum": 0,
    "description": (
        "Page number as RaaS counts it; the first page is 0. Leave it out for the "
        "first page. Use it with limit to read further pages."
    ),
}

MASTER_ID_SCHEMA: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": "Only rows of this Salt master (master id).",
}

MINION_ID_SCHEMA: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": "Only rows of this minion (minion id).",
}

KEY_STATE_SCHEMA: dict[str, Any] = {
    "type": "string",
    "enum": list(KEY_STATES),
    "description": "Only minions whose key is in this state.",
}

KEY_ROW_PROPERTIES: dict[str, Any] = {
    "minion_id": _text("Minion id."),
    "master_id": _text("Master the key belongs to."),
    "key_state": {
        "type": ["string", "null"],
        "enum": [*KEY_STATES, None],
        "description": "Key state.",
    },
}
