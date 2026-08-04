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

#: Confirmation-retry bounds (#2799). ``retry_times`` is capped at 5 --
#: Nagios deployments rarely exceed ``max_check_attempts`` 3-5, and each
#: retry adds a full backoff to worst-case detection latency.
#: ``retry_backoff_seconds`` is floored at 5 s (the same
#: hammer-the-target floor as ``interval_seconds``) and capped at 300 s
#: (a confirmation slower than that should just ride the cadence).
_RETRY_TIMES_MAX = 5
_RETRY_BACKOFF_SECONDS_MIN = 5
_RETRY_BACKOFF_SECONDS_MAX = 300


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
    runner dispatches under). It is the ``sub`` every scheduled dispatch is
    audit-attributed to (``AuditLog.operator_sub`` / broadcast
    ``principal_sub``), so :meth:`SensorAdminService.create` accepts only the
    sentinel or the creating operator's own sub -- any other value is refused
    with ``sensor_identity_sub_forbidden`` (#2699). The wire model only
    length-caps it here; the ownership check lives at the service choke point
    because Pydantic has no access to the authenticated operator.
    *tenant_id* (optional) lets a platform-admin caller target another tenant;
    the boundary enforces the RBAC.
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
    retry_times: Annotated[int, Field(ge=0, le=_RETRY_TIMES_MAX)] = 0
    retry_backoff_seconds: Annotated[
        int,
        Field(ge=_RETRY_BACKOFF_SECONDS_MIN, le=_RETRY_BACKOFF_SECONDS_MAX),
    ] = 15
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
    retry_times: int
    retry_backoff_seconds: int
    last_state: Literal["ok", "degraded", "critical", "unknown", "skip"]
    last_value: Any
    last_evidence: dict[str, object] | None
    last_evaluated_at: datetime | None
    state_since: datetime | None
    # Soft-state window (#2799): the unconfirmed candidate state (never
    # ``skip`` -- a rollup-side derivation, not an evaluation outcome)
    # and how many consecutive readings have agreed on it. Exposed so
    # the pending window is observable, the way Prometheus exposes
    # ``pending`` alerts via ``ALERTS``.
    pending_state: Literal["ok", "degraded", "critical", "unknown"] | None
    pending_count: int
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
