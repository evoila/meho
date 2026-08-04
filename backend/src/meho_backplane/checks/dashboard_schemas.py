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
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from meho_backplane.checks.assertions import CheckState
from meho_backplane.db.models import SensorSeverity, SensorStatus

__all__ = [
    "DashboardCreate",
    "DashboardDetail",
    "DashboardListResponse",
    "DashboardMemberView",
    "DashboardRead",
    "NotifyMinState",
]

#: The notification floor an operator may set on a Dashboard (#2719) -- the
#: wire twin of ``db.models._CHECK_DASHBOARD_NOTIFY_MIN_STATES``. Only the two
#: actionable states: ``ok`` as a floor would mail on every edge, and
#: ``skip`` / ``unknown`` are not severities a threshold is meaningful at.
NotifyMinState = Literal["degraded", "critical"]

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

#: Max length of the operator-authored investigator prompt (#2721) -- ~4 KiB,
#: the bound the column is documented at. Enforced here rather than by a DB
#: CHECK because this schema is the column's only writer (a Dashboard is
#: set-at-create-only), and a boundary rejection is a structured 422 naming
#: the field, its ``string_too_long`` type, and the limit in ``ctx``, where a
#: CHECK violation would surface as an opaque 500. The bound is in characters,
#: which is also what the briefing's token budget tracks.
_INVESTIGATOR_PROMPT_MAX_LENGTH = 4096

#: Max recipients a Dashboard's ``notify_email`` may fan out to (#2764). The
#: same closed-list posture as ``sensor_ids`` (``_MAX_MEMBERS``), scaled to the
#: notification surface: a channel address plus a handful of individuals is the
#: shape the ops report described, and a Dashboard mailing more than this many
#: distinct mailboxes wants a distribution-list alias, not N recipients pinned
#: on the row. Bounds both the comma-joined ``text`` column and the per-send
#: fan-out, restoring the implicit single-address bound the field carried
#: before it accepted a list.
_MAX_NOTIFY_RECIPIENTS = 16

#: Per-entry validator reused for the comma-separated ``notify_email`` list
#: (#2764). Built once at import. ``EmailStr`` (``email-validator``) lower-cases
#: the domain, strips surrounding whitespace, and returns the validated address
#: as a plain ``str``; an unparseable entry raises
#: :class:`~pydantic.ValidationError`, which the field validator re-raises as a
#: boundary 422 naming the offending entry.
_EMAIL_ADAPTER: TypeAdapter[str] = TypeAdapter(EmailStr)


class DashboardCreate(BaseModel):
    """Request body for ``POST /api/v1/checks/dashboards``.

    Membership is set at create only (no PUT; "edit" is delete + recreate,
    the trigger-immutability posture). An empty ``sensor_ids`` is permitted --
    a member-less Dashboard rolls up to ``unknown`` (the zero-member rule) --
    but duplicates are de-duplicated by the service before insert. A foreign
    or absent sensor id is refused 422 ``sensor_not_found`` at the boundary.

    *tenant_id* (optional) lets a platform-admin caller target another
    tenant; the boundary enforces the RBAC via ``authorize_tenant_scope``.

    ``notify_email`` / ``notify_min_state`` are the #2719 notification config,
    set at create only like membership. Omitting ``notify_email`` leaves
    notifications off for this Dashboard. Since #2764 it carries **one or more**
    comma-separated recipients: each entry is validated individually with
    pydantic's ``EmailStr`` (``email-validator``), so a single malformed entry
    is a boundary 422 naming it rather than a delivery failure discovered hours
    later on the first transition. The normalised, comma-joined form is what
    persists (the existing ``text`` column from migration ``0068``, no new
    migration), and a lone address is stored and read back unchanged.

    ``investigator_prompt`` is the #2721 operator context appended to the
    diagnose-only investigator's briefing. Bounded at
    ``_INVESTIGATOR_PROMPT_MAX_LENGTH``; oversize is a structured 422
    (``string_too_long`` with the limit in ``ctx``) rather than a truncation
    the operator never learns about.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=_NAME_MAX_LENGTH)
    description: str | None = Field(default=None, max_length=_DESCRIPTION_MAX_LENGTH)
    sensor_ids: list[uuid.UUID] = Field(default_factory=list, max_length=_MAX_MEMBERS)
    tenant_id: uuid.UUID | None = None
    #: One or more comma-separated recipients for transition + finding mail;
    #: ``None`` disables notification. Each entry is ``EmailStr``-validated by
    #: :meth:`_validate_notify_email` and the normalised set persists
    #: comma-joined (#2764).
    notify_email: str | None = None
    #: Floor an edge must reach before mail is sent, under
    #: ``ok < degraded < critical`` applied to ``max(previous, current)``.
    notify_min_state: NotifyMinState = "critical"
    #: Operator context appended after the server-built briefing snapshot;
    #: ``None`` leaves the briefing exactly as it was pre-#2721.
    investigator_prompt: str | None = Field(
        default=None, max_length=_INVESTIGATOR_PROMPT_MAX_LENGTH
    )

    @field_validator("notify_email", mode="after")
    @classmethod
    def _validate_notify_email(cls, value: str | None) -> str | None:
        """Validate ``notify_email`` as a comma-separated recipient list (#2764).

        ``None`` stays ``None`` (notifications off). Otherwise the value is
        split on commas, each entry is individually validated as an
        ``EmailStr`` -- so one malformed address is a 422 naming it, not a
        delivery failure found on the first transition -- and the normalised
        entries are re-joined for storage in the existing ``text`` column. A
        single address is stored and read back unchanged; a present-but-empty
        value (empty or all-blank / comma-only string) is refused, since the
        way to disable notifications is to omit the field, not to send a blank
        recipient. Bounded at :data:`_MAX_NOTIFY_RECIPIENTS`.
        """
        if value is None:
            return None
        entries = [part.strip() for part in value.split(",") if part.strip()]
        if not entries:
            raise ValueError(
                "notify_email must carry at least one address; omit the field "
                "to disable notifications"
            )
        if len(entries) > _MAX_NOTIFY_RECIPIENTS:
            raise ValueError(
                f"notify_email accepts at most {_MAX_NOTIFY_RECIPIENTS} "
                f"recipients; got {len(entries)}"
            )
        validated: list[str] = []
        for entry in entries:
            try:
                validated.append(_EMAIL_ADAPTER.validate_python(entry))
            except ValidationError as exc:
                raise ValueError(f"{entry!r} is not a valid email address") from exc
        return ",".join(validated)


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
    #: The #2719 notification config as persisted. ``notify_email`` is typed
    #: ``str`` rather than ``EmailStr`` on the read side deliberately: the
    #: address was validated on the way in, and re-validating a stored value
    #: on every read would turn a row that somehow got past the boundary into
    #: a 500 on an unrelated list call.
    notify_email: str | None
    notify_min_state: NotifyMinState
    #: The #2721 operator context as persisted. Unbounded on the read side for
    #: the same reason ``notify_email`` is a plain ``str`` here: the value was
    #: bounded on the way in, and re-validating a stored one on every read
    #: would turn a row that somehow got past the boundary into a 500 on an
    #: unrelated list call.
    investigator_prompt: str | None
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
