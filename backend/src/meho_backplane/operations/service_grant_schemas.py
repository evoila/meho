# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pydantic v2 shapes for the service-principal standing-grant surface (#3151).

Three shapes for the operator-only REST surface
(:mod:`meho_backplane.api.v1.service_grants`):

* :class:`ServiceGrantCreate` — body for creating a grant. Creating a
  grant IS the operator's upfront approval, so ``reason`` is **required**;
  ``op_id`` and ``connector_id`` are exact (no glob). The target scope is a
  concrete ``target_id``, a ``target_product`` / ``target_name_pattern``
  **selector** (#3349, resolved at dispatch time against targets that need
  not exist yet), or neither (a targetless / tenant-wide op).
* :class:`ServiceGrantRead` — row shape every accessor returns.
* :class:`ServiceGrantListResponse` — list-endpoint envelope.

All shapes set ``extra="forbid"`` so an unknown field is a 422 at the
boundary rather than a silent no-op — the same strictness the agent-grant
schemas apply.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "ServiceGrantCreate",
    "ServiceGrantListResponse",
    "ServiceGrantRead",
]


class ServiceGrantCreate(BaseModel):
    """Body for creating a standing scoped auto-approval grant.

    ``extra="forbid"`` rejects unknown fields with 422. ``op_id`` and
    ``connector_id`` name one exact operation on one exact connector, with
    deliberately **no wildcards**. The target scope is one of three shapes:

    * a concrete ``target_id`` (the op on exactly that target),
    * a **selector** — ``target_product`` and/or ``target_name_pattern``
      (#3349) — matching any dispatch whose target fingerprint satisfies it
      (``product`` exact + ``name`` ``fnmatch`` glob), so an operator can
      authorise an op on targets that **do not yet exist** (a blueprint that
      registers its own appliances mid-run), or
    * neither — a targetless / tenant-wide op (``target_id IS NULL`` with no
      selector).

    A selector and a concrete ``target_id`` are mutually exclusive: the
    selector stands **in place of** a concrete target. The service layer
    refuses delete-shaped ops regardless of the target scope (a grant is the
    floor of what runs unattended, not a bypass of a modeled destructive
    gate).
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
        description=(
            "Target UUID the grant is scoped to, or null for a targetless op "
            "or a selector grant. Mutually exclusive with target_product / "
            "target_name_pattern."
        ),
    )
    target_product: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        description=(
            "Target selector (#3349): match any dispatch whose target "
            "product equals this exact value (e.g. 'vmware'). Requires "
            "target_id to be null."
        ),
    )
    target_name_pattern: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        description=(
            "Target selector (#3349): match any dispatch whose target name "
            "matches this fnmatch glob (e.g. 'esx-dc*'). Requires target_id "
            "to be null. This glob is the explicit any-target request — it is "
            "never implied by a null target_id."
        ),
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

    @model_validator(mode="after")
    def _reject_target_id_with_selector(self) -> ServiceGrantCreate:
        """A concrete ``target_id`` and a selector are mutually exclusive.

        The selector stands *in place of* a concrete target (it resolves at
        dispatch time against targets that may not exist yet), so combining
        the two is a contradiction the create-time review must reject rather
        than silently ignore one.
        """
        if self.target_id is not None and (
            self.target_product is not None or self.target_name_pattern is not None
        ):
            raise ValueError(
                "target_id is mutually exclusive with a target selector "
                "(target_product / target_name_pattern); a selector resolves "
                "at dispatch time in place of a concrete target — set one or "
                "the other, not both"
            )
        return self


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
    # Target selector (#3349) — non-null on a selector grant, so a reader can
    # tell a selector grant from a concrete-target / targetless one.
    target_product: str | None
    target_name_pattern: str | None
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
