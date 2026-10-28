# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pydantic v2 shapes for the service-principal read scope (#4992).

Three shapes for the people-only REST surface
(:mod:`meho_backplane.api.v1.service_read_scopes`):

* :class:`ReadScopeEntry` -- one entry of a scope: exactly one of a target
  id, a target selector (``target_product`` and/or ``target_name_pattern``,
  the same shapes a standing grant uses), or ``no_target``.
* :class:`ServiceReadScopeSet` -- body for setting the whole scope.
* :class:`ServiceReadScopeRead` -- the stored scope.

All shapes set ``extra="forbid"`` so an unknown field is a 422 at the
boundary rather than a silent no-op, like the grant schemas.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "ReadScopeEntry",
    "ServiceReadScopeRead",
    "ServiceReadScopeSet",
]

#: Upper bound on entries in one scope. Enough for any real list of
#: targets; keeps one row small.
MAX_READ_SCOPE_ENTRIES = 200


class ReadScopeEntry(BaseModel):
    """One entry of a read scope. Exactly one shape per entry.

    * ``target_id`` -- exactly that target.
    * ``target_product`` and/or ``target_name_pattern`` -- a selector:
      ``product`` exact, ``name`` an ``fnmatch`` glob. It is checked at
      call time, so a target registered after the scope was set can match.
    * ``no_target: true`` -- allow ``safe`` calls that have no target.
    """

    model_config = ConfigDict(extra="forbid")

    target_id: UUID | None = Field(default=None, description="Allow reads on exactly this target.")
    target_product: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        description="Selector: allow reads on every target whose product equals this value.",
    )
    target_name_pattern: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        description="Selector: allow reads on every target whose name matches this fnmatch glob.",
    )
    no_target: bool = Field(
        default=False,
        description="Allow safe calls that have no target. Not set: such calls are refused.",
    )

    @model_validator(mode="after")
    def _exactly_one_shape(self) -> ReadScopeEntry:
        has_id = self.target_id is not None
        has_selector = self.target_product is not None or self.target_name_pattern is not None
        if [has_id, has_selector, self.no_target].count(True) != 1:
            raise ValueError(
                "an entry is exactly one of: target_id, a selector "
                "(target_product and/or target_name_pattern), or no_target=true"
            )
        return self


class ServiceReadScopeSet(BaseModel):
    """Body for ``PUT /api/v1/service-principals/read-scopes/{principal_sub}``.

    Replaces the whole scope. At least one entry: a scope with no entry
    would refuse every read, which is a job for revoking the principal.
    """

    model_config = ConfigDict(extra="forbid")

    entries: list[ReadScopeEntry] = Field(min_length=1, max_length=MAX_READ_SCOPE_ENTRIES)


class ServiceReadScopeRead(BaseModel):
    """The stored read scope of one service principal."""

    model_config = ConfigDict(frozen=True)

    tenant_id: UUID
    principal_sub: str
    entries: list[ReadScopeEntry]
    set_by_sub: str
    set_at: datetime
