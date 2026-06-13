# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pydantic schemas for the G11.3-T5 scheduler admin surface (#826).

Wire shapes the REST + MCP + CLI surfaces share. The discriminated-union
invariant the DB enforces (``ck_scheduled_trigger_kind_fields``) is
mirrored in :class:`ScheduledTriggerCreate` via a model-level validator
so a malformed body surfaces as 422 at the boundary, not as
:class:`IntegrityError` at flush.

The schemas are read-only at the wire (``frozen=True``) so an
accidentally-mutated request body cannot drift from the value validated
on the way in. Mirrors the :mod:`meho_backplane.agents.schemas` posture.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from meho_backplane.db.models import (
    ScheduledTriggerInFlightPolicy,
    ScheduledTriggerKind,
    ScheduledTriggerStatus,
)
from meho_backplane.scheduler.cron import is_valid_cron_expr, resolve_timezone

__all__ = [
    "ScheduledTriggerCreate",
    "ScheduledTriggerListResponse",
    "ScheduledTriggerRead",
]


#: Max length of an operator-supplied cron expression. A 5-field cron
#: expression with reasonable bounded ranges stays well under 128 chars;
#: the cap protects against an adversarial / misconfigured caller
#: smuggling a multi-kilobyte string past the validator.
_CRON_EXPR_MAX_LENGTH = 128

#: Max length of an IANA timezone name. The longest IANA name today is
#: ``America/Argentina/ComodRivadavia`` (35 chars); 64 is comfortable
#: headroom.
_TIMEZONE_MAX_LENGTH = 64

#: Max length of an identity-sub string. The Keycloak ``sub`` claim is
#: a UUID-shaped value in production; 256 chars covers any tenant-side
#: customisation (longer values are almost certainly a misconfiguration).
_IDENTITY_SUB_MAX_LENGTH = 256

#: Max length of a ``work_ref`` change-ticket reference. An opaque
#: cross-system string (``"gh:evoila/meho#13"`` / a Jira key / a CR id);
#: bounded so an adversarial caller cannot smuggle a large blob past the
#: validator. Mirrors the 256-char cap the audit ``work_ref`` sink
#: applies (:class:`~meho_backplane.api.v1.audit_models`).
_WORK_REF_MAX_LENGTH = 256


class ScheduledTriggerCreate(BaseModel):
    """Request body for ``POST /api/v1/scheduler/triggers``.

    Discriminated by *kind*: exactly one of ``cron_expr`` / ``fire_at``
    / ``event_filter`` must be set, matching the DB-side
    ``ck_scheduled_trigger_kind_fields`` invariant. The
    :meth:`_validate_discriminated_union` validator enforces this at
    the wire so a malformed body surfaces as 422 rather than as a
    flush-time :class:`IntegrityError`.

    *agent_definition_id* is the existing-definition reference -- the
    repository's FK check rejects an orphan id with 422 at insert; no
    validation is duplicated here.

    *identity_sub* is the OIDC ``sub`` the scheduler impersonates when
    firing the trigger (distinct from *created_by_sub* which the
    boundary derives from the operator). Defaulted to
    ``"__scheduler__"`` to match the migration-time backstop the
    ORM-level default sets, so a minimal create body still validates.

    *inputs* is optional and unvalidated here **by design**. Whether a
    trigger needs a user prompt depends on the referenced agent
    definition, which this pure wire-shape validator does not (and must
    not) load -- the definition FK is checked one layer down in the
    service. A no-inputs trigger is therefore *accepted* at create and the
    no-usable-prompt case is handled at fire time: the scheduled-run seam
    finalises the run ``failed`` with a typed
    :data:`~meho_backplane.agent.run.SCHEDULED_RUN_NO_INPUT_CLASS` error
    rather than letting it reach the provider as an empty-``messages`` 400
    (#1505). This keeps a definition that legitimately needs no user turn
    from being over-rejected at create while still surfacing the doomed
    no-prompt fire as a typed, greppable failure.

    *in_flight_policy* defaults to ``fail_into_audit`` per the consumer
    doc; operators wanting at-least-once semantics opt into ``resume``.

    *tenant_id* (optional) lets a tenant-admin caller create triggers in
    another tenant for cross-tenant admin operations; ``operator`` role
    callers leave it ``None`` and the boundary pins it to the JWT's
    tenant id. The boundary enforces the RBAC -- this schema only
    carries the field.

    *work_ref* (optional, work_ref I3-T3 #1663) is the opaque external
    change-ticket reference the trigger -- and every run it dispatches --
    works under. Pinned on the trigger row at create time and inherited
    by each dispatched run's ``agent_run.work_ref`` and audit rows via
    the shared ``work_ref_var`` ContextVar. Set-at-create-only: triggers
    have no UPDATE path. Omit when the trigger carries no change ticket.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ScheduledTriggerKind
    agent_definition_id: uuid.UUID
    cron_expr: Annotated[str | None, Field(max_length=_CRON_EXPR_MAX_LENGTH)] = None
    fire_at: datetime | None = None
    event_filter: dict[str, object] | None = None
    timezone: Annotated[str, Field(max_length=_TIMEZONE_MAX_LENGTH)] = "UTC"
    inputs: dict[str, object] | None = None
    identity_sub: Annotated[str, Field(max_length=_IDENTITY_SUB_MAX_LENGTH)] = "__scheduler__"
    in_flight_policy: ScheduledTriggerInFlightPolicy = (
        ScheduledTriggerInFlightPolicy.FAIL_INTO_AUDIT
    )
    tenant_id: uuid.UUID | None = None
    work_ref: Annotated[str | None, Field(max_length=_WORK_REF_MAX_LENGTH)] = None

    @model_validator(mode="after")
    def _validate_discriminated_union(self) -> ScheduledTriggerCreate:
        """Enforce exactly-one-discriminator + per-kind field validation.

        The DB's ``ck_scheduled_trigger_kind_fields`` ``CHECK`` is the
        ultimate guard; this validator surfaces a clear 422 at the
        boundary so an operator does not have to read an
        :class:`IntegrityError` chain to find the mistake. Validates
        the cron expression and timezone at wire time so a syntactically
        invalid cron string never reaches the repository (which would
        also raise but only after the insert was attempted).
        """
        if self.kind == ScheduledTriggerKind.CRON:
            if not self.cron_expr:
                raise ValueError("cron triggers require cron_expr")
            if self.fire_at is not None or self.event_filter is not None:
                raise ValueError(
                    "cron triggers must leave fire_at and event_filter null",
                )
            if not is_valid_cron_expr(self.cron_expr):
                raise ValueError(f"invalid cron expression: {self.cron_expr!r}")
            # ``resolve_timezone`` raises :class:`InvalidTimezoneError`
            # (a ``ValueError`` subclass) on an unknown IANA name; let
            # the exception propagate so Pydantic renders it as 422.
            resolve_timezone(self.timezone)
        elif self.kind == ScheduledTriggerKind.ONE_OFF:
            if self.fire_at is None:
                raise ValueError("one-off triggers require fire_at")
            if self.cron_expr is not None or self.event_filter is not None:
                raise ValueError(
                    "one-off triggers must leave cron_expr and event_filter null",
                )
        elif self.kind == ScheduledTriggerKind.EVENT:
            if self.event_filter is None:
                raise ValueError("event triggers require event_filter")
            if self.cron_expr is not None or self.fire_at is not None:
                raise ValueError(
                    "event triggers must leave cron_expr and fire_at null",
                )
        return self


class ScheduledTriggerRead(BaseModel):
    """Response shape for one ``scheduled_trigger`` row.

    Mirrors :class:`~meho_backplane.db.models.ScheduledTrigger`'s column
    set, projected to the wire types the JSON renderer can serialise.
    ``frozen=True`` matches the :mod:`meho_backplane.agents.schemas`
    posture so a route handler cannot accidentally mutate the row after
    returning it.
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    agent_definition_id: uuid.UUID
    kind: ScheduledTriggerKind
    cron_expr: str | None
    timezone: str
    fire_at: datetime | None
    event_filter: dict[str, object] | None
    status: ScheduledTriggerStatus
    in_flight_policy: ScheduledTriggerInFlightPolicy
    next_fire_at: datetime | None
    last_fired_at: datetime | None
    inputs: dict[str, object] | None
    identity_sub: str
    created_by_sub: str
    work_ref: str | None
    created_at: datetime
    updated_at: datetime


class ScheduledTriggerListResponse(BaseModel):
    """Response envelope for ``GET /api/v1/scheduler/triggers``.

    Wrapped in ``{"triggers": [...]}`` so a future paging / cursor
    field can land non-breakingly -- the same shape
    :class:`~meho_backplane.api.v1.agents.AgentDefinitionListResponse`
    adopted.
    """

    model_config = ConfigDict(frozen=True)

    triggers: list[ScheduledTriggerRead]


#: Re-exported sentinel kind literal for query-string filter handling
#: at the REST boundary. A consumer can pass ``kind=cron|one_off|event``
#: as a query param; the route validates against this Literal so a typo
#: surfaces as 422 rather than silently filtering nothing.
KindFilter = Literal["cron", "one_off", "event"]

#: Re-exported sentinel status literal for query-string filter handling.
StatusFilter = Literal["active", "paused", "cancelled", "fired"]
