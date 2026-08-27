# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pydantic v2 shapes for the service-principal standing-grant surface (#3151).

Three shapes for the operator-only REST surface
(:mod:`meho_backplane.api.v1.service_grants`):

* :class:`ServiceGrantCreate` — body for creating a grant. Creating a
  grant IS the operator's upfront approval, so ``reason`` is **required**;
  ``op_id`` and ``connector_id`` are exact (no glob), and ``target_id`` is
  the explicit target UUID or ``None`` for a targetless / tenant-wide op.
* :class:`ServiceGrantRead` — row shape every accessor returns.
* :class:`ServiceGrantListResponse` — list-endpoint envelope.

All shapes set ``extra="forbid"`` so an unknown field is a 422 at the
boundary rather than a silent no-op — the same strictness the agent-grant
schemas apply.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ServiceGrantCreate",
    "ServiceGrantListResponse",
    "ServiceGrantRead",
]


class ServiceGrantCreate(BaseModel):
    """Body for creating a standing scoped auto-approval grant.

    ``extra="forbid"`` rejects unknown fields with 422. There are
    deliberately **no wildcards**: ``op_id`` and ``connector_id`` name one
    exact operation on one exact connector, and ``target_id`` is either a
    concrete target UUID or ``None`` for a targetless op. The service layer
    refuses delete-shaped ops (a grant is the floor of what runs
    unattended, not a bypass of a modeled destructive gate).
    """

    model_config = ConfigDict(extra="forbid")

    principal_sub: str = Field(
        min_length=1,
        max_length=512,
        description="JWT sub of the service principal the grant authorises (no wildcard).",
    )
    op_id: str = Field(
        min_length=1,
        max_length=512,
        description="Exact operation id, no glob (e.g. 'POST:/vcenter/vm').",
    )
    connector_id: str = Field(
        min_length=1,
        max_length=256,
        description="Exact '<impl_id>-<version>' connector id, e.g. 'vmware-rest-9.0'.",
    )
    target_id: UUID | None = Field(
        default=None,
        description="Target UUID the grant is scoped to, or null for a targetless op.",
    )
    reason: str = Field(
        min_length=1,
        max_length=2048,
        description="Operator's upfront justification (creating the grant is the review).",
    )
    expires_at: datetime | None = Field(
        default=None,
        description="Optional UTC expiry; null = standing (permanent) grant; must be future.",
    )


class ServiceGrantRead(BaseModel):
    """Row shape every accessor returns.

    ``from_attributes=True`` allows direct construction from an ORM row.
    Exposes ``revoked_at`` / ``revoked_by_sub`` so callers can tell a live
    grant from a revoked one in the history.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    principal_sub: str
    op_id: str
    connector_id: str
    target_id: UUID | None
    reason: str
    created_by_sub: str
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    revoked_by_sub: str | None


class ServiceGrantListResponse(BaseModel):
    """Response envelope for ``GET /api/v1/service-principals/grants``."""

    model_config = ConfigDict(frozen=True)

    grants: list[ServiceGrantRead]
