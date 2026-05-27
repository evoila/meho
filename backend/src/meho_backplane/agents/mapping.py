# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Payload -> ORM mappers for ``AgentDefinition`` write paths.

G11.2-T9 (#1112) extracted these out of
:mod:`meho_backplane.agents.service` so the service module stays
inside the per-file size budget. Both helpers are pure: they
translate Pydantic-validated create/update payloads onto SQLAlchemy
rows. ``model_tier`` is round-tripped through its ``StrEnum``
``.value`` on both paths so the column stores the wire string
(``"deep"``) rather than the enum repr (``"AgentModelTier.DEEP"``).
"""

from __future__ import annotations

import uuid

from meho_backplane.agents.schemas import AgentDefinitionCreate
from meho_backplane.db.models import AgentDefinition

__all__ = [
    "apply_changes",
    "build_definition_row",
]


def build_definition_row(
    tenant_id: uuid.UUID,
    created_by_sub: str,
    payload: AgentDefinitionCreate,
) -> AgentDefinition:
    """Map a validated create payload onto a fresh :class:`AgentDefinition` row."""
    return AgentDefinition(
        tenant_id=tenant_id,
        name=payload.name,
        identity_ref=payload.identity_ref,
        model_tier=payload.model_tier.value,
        system_prompt=payload.system_prompt,
        toolset=payload.toolset,
        turn_budget=payload.turn_budget,
        output_schema=payload.output_schema,
        enabled=payload.enabled,
        created_by_sub=created_by_sub,
    )


def apply_changes(row: AgentDefinition, changes: dict[str, object]) -> None:
    """Apply a validated PATCH body to *row* (``model_tier`` -> ``.value``)."""
    for field, value in changes.items():
        if field == "model_tier" and value is not None:
            value = value.value if hasattr(value, "value") else value
        setattr(row, field, value)
