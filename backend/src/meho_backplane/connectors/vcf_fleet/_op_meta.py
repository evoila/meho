# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The metadata record of one typed Fleet op.

It lives in its own module so every op module (``typed_ops`` and
``typed_ops_locker``) can use it without importing the other.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

__all__ = ["FleetTypedOp"]


@dataclass(frozen=True)
class FleetTypedOp:
    """Metadata for one typed Fleet op the connector registers at startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so the registrar can splat the dataclass into the helper
    without per-op boilerplate. ``handler_attr`` is the attribute name on
    :class:`~meho_backplane.connectors.vcf_fleet.connector.VcfFleetConnector`
    exposing the async handler; the registrar resolves the bound method
    against the class at registration time so the dispatcher can recover
    the callable from the persisted ``module.ClassName.method`` path.
    """

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
