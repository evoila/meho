# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pydantic wire shapes for the Sensor admin surface (#2503).

Wire shapes the REST + MCP + CLI surfaces share for the ``sensor`` entity
(Initiative #2416, parent goal #221). Mirrors the
:mod:`meho_backplane.scheduler.schemas` posture: frozen models
(``frozen=True``) so a request body cannot drift from the value validated
on the way in, and a model-level validator that enforces the DB's cadence
discriminated-union invariant (``ck_sensor_cadence_fields``) at the
boundary -- a malformed body is a 422 at create, not an
:class:`~sqlalchemy.exc.IntegrityError` at flush.

The ``assertion`` field is typed with #2504's frozen
:class:`~meho_backplane.checks.assertions.AssertionSpec`: a bad select
path or an unknown comparator ``type`` surfaces as a Pydantic 422 at the
wire (the same 422 a malformed cadence gets), and the spec models are not
re-declared here.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from meho_backplane.checks.assertions import AssertionSpec
from meho_backplane.db.models import SensorCadenceKind, SensorSeverity, SensorStatus
from meho_backplane.scheduler.cron import is_valid_cron_expr, resolve_timezone

__all__ = [
    "SensorCreate",
    "SensorListResponse",
    "SensorRead",
]

#: Max length of an operator-supplied Sensor name. Sensors are referenced
#: by name from Dashboards (#2506); 128 chars is generous for a handle and
#: bounds an adversarial caller from smuggling a multi-kilobyte string.
_NAME_MAX_LENGTH = 128

#: Max length of a ``connector_id`` / ``op_id`` string. Bounded to keep an
#: adversarial caller from smuggling a large blob past the validator; the
#: real gate is the descriptor lookup, which just misses cleanly.
_CONNECTOR_ID_MAX_LENGTH = 256
_OP_ID_MAX_LENGTH = 256

#: Max length of a cron expression (mirrors the scheduler's cap).
_CRON_EXPR_MAX_LENGTH = 128

#: Max length of an IANA timezone name (mirrors the scheduler's cap).
_TIMEZONE_MAX_LENGTH = 64

#: Max length of an identity-sub string (mirrors the scheduler's cap).
_IDENTITY_SUB_MAX_LENGTH = 256

#: Interval-cadence bounds. Sub-minute is allowed (the interval-tick path
#: #2505 drives), floored at 5 s so a runaway sensor cannot hammer a
#: target every second, capped at one day.
_INTERVAL_SECONDS_MIN = 5
_INTERVAL_SECONDS_MAX = 86400

#: Serialized-size cap on the assertion spec. The spec models are already
#: bounded (one select + one typed comparator), but an ``in`` comparator
#: with a huge ``values`` list, or a deeply-padded payload, is capped so a
#: sensor row cannot carry an unbounded blob. 8 KiB is comfortably above
#: any realistic bounded assertion.
_ASSERTION_MAX_SERIALIZED_BYTES = 8192


class SensorCreate(BaseModel):
    """Request body for ``POST /api/v1/sensors``.

    Discriminated by *cadence_kind*: exactly one of ``interval_seconds``
    (interval cadence, 5..86400 s) / ``cron_expr`` + ``timezone`` (cron
    cadence) must be set, matching the DB-side ``ck_sensor_cadence_fields``
    invariant. The :meth:`_validate_cadence_and_assertion` validator
    enforces this at the wire, validates the cron expression + timezone
    exactly as :mod:`meho_backplane.scheduler.schemas` does, and caps the
    serialized assertion size.

    ``status`` is deliberately **not** a field: sensors are
    set-at-create-only (like scheduled triggers), and a row is only ever
    parked (``status='paused'``) by #2505's runner, never at create. With
    ``extra="forbid"`` a body carrying ``status`` is a 422.

    *identity_sub* defaults to ``"__sensor__"`` (the sentinel #2505's
    runner dispatches under). *tenant_id* (optional) lets a platform-admin
    caller target another tenant; the boundary enforces the RBAC.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=_NAME_MAX_LENGTH)]
    connector_id: Annotated[str, Field(min_length=1, max_length=_CONNECTOR_ID_MAX_LENGTH)]
    op_id: Annotated[str, Field(min_length=1, max_length=_OP_ID_MAX_LENGTH)]
    target: dict[str, object] | None = None
    params: dict[str, object] = Field(default_factory=dict)
    assertion: AssertionSpec
    cadence_kind: SensorCadenceKind
    interval_seconds: Annotated[
        int | None,
        Field(default=None, ge=_INTERVAL_SECONDS_MIN, le=_INTERVAL_SECONDS_MAX),
    ] = None
    cron_expr: Annotated[str | None, Field(max_length=_CRON_EXPR_MAX_LENGTH)] = None
    timezone: Annotated[str, Field(max_length=_TIMEZONE_MAX_LENGTH)] = "UTC"
    severity: SensorSeverity = SensorSeverity.CRITICAL
    for_seconds: Annotated[int, Field(ge=0)] = 0
    identity_sub: Annotated[str, Field(max_length=_IDENTITY_SUB_MAX_LENGTH)] = "__sensor__"
    tenant_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _validate_cadence_and_assertion(self) -> SensorCreate:
        """Enforce the cadence union + validate cron / cap the assertion size.

        The DB's ``ck_sensor_cadence_fields`` CHECK is the ultimate guard;
        this validator surfaces a clean 422 at the boundary. For the cron
        cadence the expression and timezone are validated at wire time (the
        same shape :func:`meho_backplane.scheduler.schemas._require_cron_fields`
        uses) so a syntactically invalid cron string never reaches the
        repository.
        """
        if self.cadence_kind == SensorCadenceKind.INTERVAL:
            if self.interval_seconds is None:
                raise ValueError("interval cadence requires interval_seconds")
            if self.cron_expr is not None:
                raise ValueError("interval cadence must leave cron_expr null")
        elif self.cadence_kind == SensorCadenceKind.CRON:
            if not self.cron_expr:
                raise ValueError("cron cadence requires cron_expr")
            if self.interval_seconds is not None:
                raise ValueError("cron cadence must leave interval_seconds null")
            if not is_valid_cron_expr(self.cron_expr):
                raise ValueError(f"invalid cron expression: {self.cron_expr!r}")
            # ``resolve_timezone`` raises InvalidTimezoneError (a ValueError
            # subclass) on an unknown IANA name; let it propagate as 422.
            resolve_timezone(self.timezone)
        # Cap the serialized assertion so a bounded-but-padded spec cannot
        # carry an unbounded blob onto the row.
        serialized = json.dumps(self.assertion.model_dump(mode="json"))
        if len(serialized.encode("utf-8")) > _ASSERTION_MAX_SERIALIZED_BYTES:
            raise ValueError(
                f"assertion exceeds the {_ASSERTION_MAX_SERIALIZED_BYTES}-byte serialized cap",
            )
        return self


class SensorRead(BaseModel):
    """Response shape for one ``sensor`` row.

    Mirrors :class:`~meho_backplane.db.models.Sensor`'s column set,
    projected to the wire types the JSON renderer can serialise. Includes
    the latest-result projection (``last_state`` / ``last_value`` /
    ``last_evidence`` / ``last_evaluated_at`` / ``state_since``) so the
    list response carries the current status view (there is no REST
    GET-by-id -- the mould exposes none). ``frozen=True`` so a route
    handler cannot mutate the row after returning it.
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    connector_id: str
    op_id: str
    target: dict[str, object] | None
    params: dict[str, object]
    assertion: dict[str, object]
    status: SensorStatus
    status_reason: str | None
    cadence_kind: SensorCadenceKind
    interval_seconds: int | None
    cron_expr: str | None
    timezone: str
    next_fire_at: datetime | None
    severity: SensorSeverity
    for_seconds: int
    last_state: Literal["ok", "degraded", "critical", "unknown", "skip"]
    last_value: Any
    last_evidence: dict[str, object] | None
    last_evaluated_at: datetime | None
    state_since: datetime | None
    identity_sub: str
    created_by_sub: str
    created_at: datetime
    updated_at: datetime


class SensorListResponse(BaseModel):
    """Response envelope for ``GET /api/v1/sensors``.

    Wrapped in ``{"sensors": [...]}`` so a future paging / cursor field
    can land non-breakingly -- the same shape
    :class:`~meho_backplane.scheduler.schemas.ScheduledTriggerListResponse`
    adopted.
    """

    model_config = ConfigDict(frozen=True)

    sensors: list[SensorRead]


#: Re-exported sentinel status literal for query-string filter handling at
#: the REST boundary. A consumer can pass ``status=active|paused``; the
#: route validates against this Literal so a typo surfaces as 422.
SensorStatusFilter = Literal["active", "paused"]

#: Re-exported sentinel cadence-kind literal for query-string filtering.
SensorCadenceFilter = Literal["interval", "cron"]
