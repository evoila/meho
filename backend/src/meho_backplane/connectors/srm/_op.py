# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The metadata record of one SRM typed operation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

__all__ = ["SrmTypedOp"]


@dataclass(frozen=True)
class SrmTypedOp:
    """Metadata for one SRM typed op registered at lifespan startup."""

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
