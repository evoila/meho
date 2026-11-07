# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pydantic v2 shapes for the run approval (#4126).

Shapes for the REST surface (:mod:`meho_backplane.api.v1.run_approvals`):

* :class:`RunApprovalEntry` -- one operation the run may use: a
  ``connector_id``, an ``op_id`` and one target, a target kind
  (``target_product``) or no target.
* :class:`RunApprovalCreate` -- the request the add-on sends.
* :class:`RunApprovalRevoke` -- the optional reason of a revoke.
* :class:`RunApprovalRead` / :class:`RunApprovalListResponse` -- the
  stored request with its status.

Request shapes set ``extra="forbid"`` so an unknown field is a 422 at the
boundary, like the grant schemas.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "MAX_RUN_APPROVAL_ENTRIES",
    "RunApprovalCreate",
    "RunApprovalEntry",
    "RunApprovalListResponse",
    "RunApprovalRead",
    "RunApprovalRevoke",
    "RunApprovalStatus",
]

#: Upper bound on entries in one request. Enough for a long run; keeps the
#: list short enough for a person to read.
MAX_RUN_APPROVAL_ENTRIES = 200

#: ``pending`` waits for a person; ``approved`` covers listed calls;
#: ``revoked``, ``ended`` and ``expired`` cover nothing.
RunApprovalStatus = Literal["pending", "approved", "revoked", "ended", "expired"]


class RunApprovalEntry(BaseModel):
    """One operation the run may use.

    ``target_id`` names one target. ``target_product`` names a target kind,
    so targets the run registers later are covered; it works only when the
    tenant setting allows target kinds. With neither, the entry covers only
    calls without a target (no target is not "any target").
    """

    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(min_length=1, max_length=256)
    op_id: str = Field(min_length=1, max_length=512)
    target_id: UUID | None = Field(default=None, description="Exactly this target.")
    target_product: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        description="Every target of this product (target kind). Needs the tenant setting.",
    )

    @model_validator(mode="after")
    def _one_target_shape(self) -> RunApprovalEntry:
        if self.target_id is not None and self.target_product is not None:
            raise ValueError("an entry has target_id or target_product, not both")
        return self


class RunApprovalCreate(BaseModel):
    """Body of ``POST /api/v1/service-principals/run-approvals``."""

    model_config = ConfigDict(extra="forbid")

    work_ref: str = Field(
        min_length=1,
        max_length=256,
        description="The run reference. Calls of the run send it in the Meho-Work-Ref header.",
    )
    entries: list[RunApprovalEntry] = Field(min_length=1, max_length=MAX_RUN_APPROVAL_ENTRIES)
    reason: str = Field(
        min_length=1, max_length=2048, description="What the run does, for the approver."
    )
    expires_at: datetime | None = Field(
        default=None,
        description="When the approval ends at the latest. Default: 24 hours; at most 7 days.",
    )


class RunApprovalRevoke(BaseModel):
    """Body of ``POST /api/v1/service-principals/run-approvals/{id}/revoke``."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, min_length=1, max_length=2048)


class RunApprovalRead(BaseModel):
    """One run approval with its status."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID
    principal_sub: str
    work_ref: str
    entries: list[RunApprovalEntry]
    reason: str
    status: RunApprovalStatus
    requested_at: datetime
    expires_at: datetime
    approved_at: datetime | None = None
    approved_by_sub: str | None = None
    revoked_at: datetime | None = None
    revoked_by_sub: str | None = None
    revoked_reason: str | None = None
    ended_at: datetime | None = None


class RunApprovalListResponse(BaseModel):
    """``GET /api/v1/service-principals/run-approvals`` response."""

    model_config = ConfigDict(frozen=True)

    run_approvals: list[RunApprovalRead]
