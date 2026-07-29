# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pydantic wire shapes for the Dashboard admin surface (#2506).

Wire shapes the REST + ``/ui/checks`` surfaces share for the Dashboard
entity (Initiative #2416, parent goal #221). Mirrors the
:mod:`meho_backplane.checks.schemas` (Sensor) posture: frozen models
(``frozen=True``) so a request body cannot drift from the value validated
on the way in, and closed-vocabulary state fields typed with #2504's
:data:`~meho_backplane.checks.assertions.CheckState` (not re-declared here).

The rolled-up ``state`` on a read is **evaluated on read** by
:mod:`meho_backplane.checks.rollup`; ``last_rollup_state`` is the separate
transition-detection memo column (#2507), shipped unwritten by this Task and
surfaced here only so a read can confirm it is still NULL.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.checks.assertions import CheckState
from meho_backplane.db.models import SensorSeverity, SensorStatus

__all__ = [
    "DashboardCreate",
    "DashboardDetail",
    "DashboardListResponse",
    "DashboardMemberView",
    "DashboardRead",
]

#: Max length of an operator-supplied Dashboard name (mirrors the Sensor cap).
_NAME_MAX_LENGTH = 128

#: Max length of a Dashboard description -- bounds an adversarial caller from
#: smuggling a multi-kilobyte blob onto the row.
_DESCRIPTION_MAX_LENGTH = 2048

#: Max member Sensors a single Dashboard may compose. A Dashboard is a
#: glance surface; a caller wiring hundreds of members has a composition
#: problem the rollup is not the place to fix, and the cap bounds the
#: create-time validation fan-out.
_MAX_MEMBERS = 200


class DashboardCreate(BaseModel):
    """Request body for ``POST /api/v1/checks/dashboards``.

    Membership is set at create only (no PUT; "edit" is delete + recreate,
    the trigger-immutability posture). An empty ``sensor_ids`` is permitted --
    a member-less Dashboard rolls up to ``unknown`` (the zero-member rule) --
    but duplicates are de-duplicated by the service before insert. A foreign
    or absent sensor id is refused 422 ``sensor_not_found`` at the boundary.

    *tenant_id* (optional) lets a platform-admin caller target another
    tenant; the boundary enforces the RBAC via ``authorize_tenant_scope``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=_NAME_MAX_LENGTH)
    description: str | None = Field(default=None, max_length=_DESCRIPTION_MAX_LENGTH)
    sensor_ids: list[uuid.UUID] = Field(default_factory=list, max_length=_MAX_MEMBERS)
    tenant_id: uuid.UUID | None = None


class DashboardMemberView(BaseModel):
    """One member Sensor as rendered on a Dashboard detail read.

    Carries the raw + rolled-up per-member states (``raw_state`` /
    ``effective_state`` / ``pending`` from
    :mod:`meho_backplane.checks.rollup`) plus the Sensor context an operator
    needs to act -- the op identity, the severity cap, the ``for:`` window,
    the hysteresis clock (``state_since``), and the last observed value /
    evidence.
    """

    model_config = ConfigDict(frozen=True)

    sensor_id: uuid.UUID
    name: str
    connector_id: str
    op_id: str
    #: The member's current derived state (paused -> ``skip``, stale ->
    #: ``unknown``, else the persisted ``last_state``).
    raw_state: CheckState
    #: What the member contributes to the fold (``skip`` excluded, ``ok``
    #: when healthy or held-pending, else the severity-capped state).
    effective_state: CheckState
    #: ``True`` when a failing raw state is being held by the ``for:`` window.
    pending: bool
    severity: SensorSeverity
    for_seconds: int
    status: SensorStatus
    state_since: datetime | None
    last_value: Any
    last_evidence: dict[str, object] | None
    last_evaluated_at: datetime | None
    next_fire_at: datetime | None


class DashboardRead(BaseModel):
    """Response shape for one Dashboard row on the list surface.

    Carries the rolled-up ``state`` (evaluated on read) and the
    ``member_count`` so the list answers "is everything OK?" per Dashboard
    without a detail fetch. ``last_rollup_state`` is the memo column (always
    NULL until #2507 writes it). ``frozen=True`` so a handler cannot mutate
    the row after returning it.
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    description: str | None
    member_count: int
    #: The five-state worst-of rollup, evaluated on read.
    state: CheckState
    #: The transition-detection memo column (#2507); NULL until then.
    last_rollup_state: CheckState | None
    created_by_sub: str
    created_at: datetime
    updated_at: datetime


class DashboardDetail(DashboardRead):
    """Response shape for ``GET /api/v1/checks/dashboards/{id}``.

    Extends :class:`DashboardRead` with the per-member breakdown the console
    detail page + the REST detail expose.
    """

    members: list[DashboardMemberView]


class DashboardListResponse(BaseModel):
    """Response envelope for ``GET /api/v1/checks/dashboards``.

    Wrapped in ``{"dashboards": [...]}`` so a future paging / cursor field
    can land non-breakingly -- the same shape the Sensor list adopts.
    """

    model_config = ConfigDict(frozen=True)

    dashboards: list[DashboardRead]
