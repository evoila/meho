# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The metadata record of one fleet-lcm typed operation.

It lives in its own module so :mod:`.typed_ops` (the reads and the registrar)
and :mod:`.lifecycle_ops` (the changes) can both build rows without importing
each other.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

__all__ = ["FleetLcmTypedOp"]


@dataclass(frozen=True)
class FleetLcmTypedOp:
    """Metadata for one Fleet LCM typed op registered at lifespan startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so :func:`~.typed_ops.register_fleet_lcm_typed_operations` can
    splat the dataclass into the helper without per-op boilerplate.
    ``handler_attr`` is the attribute name on :class:`FleetLcmConnector`
    exposing the async handler; the registrar resolves the bound method
    against the class at registration time.
    """

    op_id: str
    handler_attr: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any] | None
    group_key: str
    tags: tuple[str, ...]
    safety_level: Literal["safe", "caution", "dangerous", "destructive"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None
