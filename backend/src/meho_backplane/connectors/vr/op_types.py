# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op metadata type and the group keys of the vSphere Replication connector."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal

__all__ = [
    "GROUP_API_DOCS",
    "GROUP_PAIRINGS",
    "GROUP_REPLICATIONS",
    "GROUP_SERVER",
    "GROUP_TASKS",
    "VrTypedOp",
]

GROUP_SERVER: Final = "vr-server"
GROUP_API_DOCS: Final = "vr-api-docs"
GROUP_PAIRINGS: Final = "vr-pairings"
GROUP_REPLICATIONS: Final = "vr-replications"
GROUP_TASKS: Final = "vr-tasks"


@dataclass(frozen=True)
class VrTypedOp:
    """Metadata for one vSphere Replication typed op registered at lifespan startup."""

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
