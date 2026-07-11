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

#: Shared wire-description fragment naming the scheduler's tick-quantization
#: latency contract, surfaced on the timestamp fields so REST / MCP / CLI
#: consumers can plan SLAs against the grid. The loop scans on a fixed cadence
#: every ``SCHEDULER_TICK_INTERVAL_SECONDS`` and claims rows whose
#: ``next_fire_at`` is at or before the tick instant
#: (:func:`~meho_backplane.scheduler.repository.claim_due_triggers`), so a
#: requested time is a floor, not an exact dispatch instant: a fire lands on the
#: first tick at or after it, worst-case one whole interval later. See
#: ``docs/codebase/scheduler.md``.
_TICK_LATENCY_NOTE = (
    "The scheduler scans on a fixed grid every SCHEDULER_TICK_INTERVAL_SECONDS "
    "(default 30 s, env-tunable 1-3600 s) and fires on the first tick at or "
    "after this time -- so it is a floor, not an exact dispatch instant, and "
    "actual dispatch can trail it by up to one tick interval. SLA-sensitive "
    "deployments lower the tick interval (floor 1 s) per deployment."
)


def _payload_yields_prompt(inputs: dict[str, object] | None) -> bool:
    """Return ``True`` when *inputs* renders a non-empty user prompt.

    Payload-only mirror of the fire-time pair
    :func:`~meho_backplane.scheduler.loop._coerce_inputs` +
    :func:`~meho_backplane.agent.run.prompt_is_effectively_empty`, inlined
    here so this pure wire-shape module stays free of an import into the
    loop / invocation layer. The rendering contract mirrored: the
    conventional ``"prompt"`` string key when present, else the dict
    JSON-dumps to a non-empty payload the runtime forwards verbatim.

    One deliberate tightening over the fire-time pair: an empty dict counts
    as *no* prompt. ``_coerce_inputs`` renders ``{}`` to the literal string
    ``"{}"``, which is non-whitespace and so slips past the fire-time
    :func:`prompt_is_effectively_empty` guard and reaches the model as a
    meaningless ``"{}"`` user turn; rejecting it at create closes that edge.
    """
    if not inputs:  # None or an empty dict -> no user turn
        return False
    prompt = inputs.get("prompt")
    if isinstance(prompt, str):
        return bool(prompt.strip())
    # A non-empty dict without a string ``prompt`` key JSON-dumps to a
    # non-empty payload (the ``_coerce_inputs`` fallback) -- a usable turn.
    return True


def _require_cron_fields(model: ScheduledTriggerCreate) -> None:
    """Validate the ``cron`` discriminator fields (helper for the validator)."""
    if not model.cron_expr:
        raise ValueError("cron triggers require cron_expr")
    if model.fire_at is not None or model.event_filter is not None:
        raise ValueError("cron triggers must leave fire_at and event_filter null")
    if not is_valid_cron_expr(model.cron_expr):
        raise ValueError(f"invalid cron expression: {model.cron_expr!r}")
    # ``resolve_timezone`` raises :class:`InvalidTimezoneError` (a ``ValueError``
    # subclass) on an unknown IANA name; let it propagate so Pydantic renders 422.
    resolve_timezone(model.timezone)


def _require_one_off_fields(model: ScheduledTriggerCreate) -> None:
    """Validate the ``one_off`` discriminator fields (helper for the validator)."""
    if model.fire_at is None:
        raise ValueError("one-off triggers require fire_at")
    if model.cron_expr is not None or model.event_filter is not None:
        raise ValueError("one-off triggers must leave cron_expr and event_filter null")


def _require_event_fields(model: ScheduledTriggerCreate) -> None:
    """Validate the ``event`` discriminator fields (helper for the validator)."""
    if model.event_filter is None:
        raise ValueError("event triggers require event_filter")
    if model.cron_expr is not None or model.fire_at is not None:
        raise ValueError("event triggers must leave cron_expr and fire_at null")


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

    *inputs* is the JSON payload rendered into the run's user-prompt string
    at fire time (:func:`~meho_backplane.scheduler.loop._coerce_inputs`).
    For ``kind=cron`` and ``kind=one_off`` it **must** render a non-empty
    user turn: :meth:`_validate_discriminated_union` rejects an input-less
    trigger -- and the ``inputs: {}`` case, which would otherwise render the
    literal ``"{}"`` -- with a 422 at create. The check is payload-only
    (:func:`_payload_yields_prompt`); it loads no agent definition, so it
    does not resurrect the layering objection that kept #1505 fire-time-only
    -- a cron that fires every tick and a one_off that burns its single fire
    with no user turn are deterministic failures the wire shape can see
    without extra I/O. ``kind=event`` is **exempt**: its future
    payload-dispatch junction may derive the prompt from the matched event,
    so an input-less event trigger stays creatable.

    The fire-time typed guard remains as defense-in-depth
    (:data:`~meho_backplane.agent.run.SCHEDULED_RUN_NO_INPUT_CLASS`, #1505):
    it still finalises a no-prompt fire ``failed`` before the model call for
    ``event`` triggers and for any row inserted directly around this wire
    schema. See ``docs/codebase/scheduler.md``.

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
    fire_at: Annotated[
        datetime | None,
        Field(
            description=(
                "One-off fire time (UTC); required for kind=one_off and must be "
                "null for cron / event triggers. " + _TICK_LATENCY_NOTE
            ),
        ),
    ] = None
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
        also raise but only after the insert was attempted). Also rejects a
        ``cron`` / ``one_off`` trigger whose *inputs* render no usable prompt
        (payload-only; see :func:`_payload_yields_prompt`), so an input-less
        trigger doomed to fail every fire is caught at create.
        """
        if self.kind == ScheduledTriggerKind.CRON:
            _require_cron_fields(self)
        elif self.kind == ScheduledTriggerKind.ONE_OFF:
            _require_one_off_fields(self)
        elif self.kind == ScheduledTriggerKind.EVENT:
            _require_event_fields(self)
        # A cron / one_off trigger with no usable prompt is a deterministic
        # failure the payload alone reveals: reject it at create rather than
        # letting it fire uselessly (a cron every tick, a one_off once) and
        # fail typed. Payload-only -- no definition load -- so #1505's
        # layering objection does not apply. ``event`` is exempt (its future
        # dispatch junction may derive the prompt from the matched event).
        if self.kind in (ScheduledTriggerKind.CRON, ScheduledTriggerKind.ONE_OFF) and (
            not _payload_yields_prompt(self.inputs)
        ):
            raise ValueError(
                "cron and one_off triggers require inputs that render a "
                "non-empty user prompt: set inputs.prompt to a non-empty "
                "string, or provide a non-empty inputs payload (an input-less "
                "trigger, or inputs: {}, has no user turn and would fail "
                "every fire)",
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
    fire_at: Annotated[
        datetime | None,
        Field(
            description=(
                "Stored one-off fire time (UTC), echoed from create; null for "
                "cron / event triggers. " + _TICK_LATENCY_NOTE
            ),
        ),
    ]
    event_filter: dict[str, object] | None
    status: ScheduledTriggerStatus
    in_flight_policy: ScheduledTriggerInFlightPolicy
    next_fire_at: Annotated[
        datetime | None,
        Field(
            description=(
                "Next instant the trigger is eligible to fire (UTC) -- the column "
                "the tick loop scans; null for event triggers (dispatched on event "
                "arrival, not the clock). " + _TICK_LATENCY_NOTE
            ),
        ),
    ]
    last_fired_at: Annotated[
        datetime | None,
        Field(
            description=(
                "Timestamp of the most recent fire (UTC), stamped with the "
                "scheduler tick that claimed the row -- not the trigger's fire_at "
                "/ next_fire_at. Because fires are tick-aligned, successive "
                "last_fired_at values sit on the tick grid and can trail the "
                "requested time by up to one tick interval "
                "(SCHEDULER_TICK_INTERVAL_SECONDS, default 30 s); null until the "
                "first fire."
            ),
        ),
    ]
    last_skip_reason: Annotated[
        str | None,
        Field(
            description=(
                "Machine tag of the most recent tick the scheduler skipped this "
                "trigger without firing -- one of 'definition_missing', "
                "'definition_disabled', 'credentials_unresolved' (a park also "
                "stamps 'invalid_cron_expr' / 'unknown_kind'). null when the "
                "trigger has never skipped since its last successful fire "
                "(cleared to null on the next fire). A non-null value on an "
                "'active' trigger means it looks healthy but is silently not "
                "firing -- fix the named cause. (#2327)"
            ),
        ),
    ]
    last_skipped_at: Annotated[
        datetime | None,
        Field(
            description=(
                "UTC timestamp of the most recent skipped tick; null until the "
                "first skip and cleared on the next successful fire. (#2327)"
            ),
        ),
    ]
    skip_count: Annotated[
        int,
        Field(
            description=(
                "Consecutive ticks skipped since the last successful fire (0 when "
                "healthy; reset to 0 on the next fire). The scheduler parks the "
                "trigger ('status'='paused') once this reaches its internal "
                "consecutive-skip cap, so a permanently-unresolvable trigger "
                "stops silently re-tripping every tick. (#2327)"
            ),
        ),
    ]
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
