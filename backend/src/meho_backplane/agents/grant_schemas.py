# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pydantic v2 models for the agent permission grant surface.

G11.2-T6 (#819) under Initiative #803. Four shapes plus the
:class:`GrantVerdict` alias:

* :class:`AgentGrantCreate` — body for creating a permanent or
  time-bounded permission grant.
* :class:`AgentGrantRead` — row shape every accessor returns.
* :class:`AgentGrantListResponse` — list-endpoint envelope.
* :class:`AgentElevationCreate` — convenience alias for grants that
  carry ``expires_at``, with clearer validation messaging.

All shapes set ``extra="forbid"`` so an unknown field is a 422 at the
boundary rather than a silent no-op — the same strictness the agent
definition schemas apply.

Pattern validation
------------------

``op_pattern`` is a fnmatch glob string. The only hard constraint is
that it must be non-empty; the shape (``"*"``, ``"vault.kv.*"``,
``"GET:/api/vcenter/*"``) is validated by the service layer using
:func:`fnmatch.fnmatch`. No pre-parse is done here to keep the schema
focused on type/presence, not semantics.

``target_scope`` validation is also semantic — it must be either
``None``, ``"*"``, or a string representation of a UUID.  The service
layer performs the UUID-or-wildcard check; the schema accepts any
non-empty string so route/MCP callers get a service-level error message
rather than a pydantic-level one.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.db.models import PermissionVerdict

__all__ = [
    "AgentElevationCreate",
    "AgentGrantCreate",
    "AgentGrantListResponse",
    "AgentGrantRead",
    "GrantVerdict",
]

#: Collapsed into the single source of truth (#1052/T3): a grant carries
#: a :class:`~meho_backplane.db.models.PermissionVerdict`. ``GrantVerdict``
#: is retained as a re-export so the REST / MCP surface imports keep
#: working without constructing raw string verdicts.
GrantVerdict = PermissionVerdict


class AgentGrantCreate(BaseModel):
    """Body for creating a permission grant (permanent or time-bounded).

    ``extra="forbid"`` rejects unknown fields with 422. ``expires_at``
    is optional — omit it for a permanent grant, supply a future UTC
    datetime for a time-bounded elevation.
    """

    model_config = ConfigDict(extra="forbid")

    principal_sub: str = Field(
        min_length=1,
        max_length=512,
        description="JWT sub of the principal being granted the permission.",
    )
    op_pattern: str = Field(
        min_length=1,
        max_length=512,
        description="fnmatch glob matching operation IDs. '*' = all ops.",
    )
    target_scope: str | None = Field(
        default=None,
        description=(
            "Target UUID, '*', or null (null = any target). "
            "A non-null non-wildcard value must be a valid UUID string."
        ),
    )
    verdict: GrantVerdict = Field(
        description="One of auto-execute | needs-approval | deny.",
    )
    expires_at: datetime | None = Field(
        default=None,
        description=(
            "UTC expiry timestamp for time-bounded elevations. "
            "Null = permanent grant. Must be in the future."
        ),
    )


class AgentElevationCreate(AgentGrantCreate):
    """Convenience alias for time-bounded elevations.

    Identical to :class:`AgentGrantCreate` but ``expires_at`` is
    required. The REST surface accepts both; the MCP surface exposes
    a dedicated ``meho.agents.grant.elevate`` tool that uses this schema
    so the field-level validation message is specific:
    "expires_at is required for elevations".
    """

    expires_at: datetime = Field(
        description=("Required UTC expiry timestamp for the elevation. Must be a future datetime."),
    )


class AgentGrantRead(BaseModel):
    """Row shape every accessor returns.

    ``from_attributes=True`` allows direct construction from an ORM
    row. Exposes ``expires_at`` so callers can distinguish permanent
    grants from elevations.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    principal_sub: str
    op_pattern: str
    target_scope: str | None
    verdict: str
    created_by_sub: str
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AgentGrantListResponse(BaseModel):
    """Response envelope for ``GET /api/v1/agents/grants``."""

    model_config = ConfigDict(frozen=True)

    grants: list[AgentGrantRead]
