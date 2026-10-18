# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Claim rules for intentional agent announcements (#3736).

An announcement from a trusted agent is one step of a claim:

* ``start`` opens a new claim. The server mints the logical
  ``claim_id``. The caller must not send one.
* ``update`` and ``completion`` continue an open claim. The caller must
  send the ``claim_id`` and an ``idempotency_key``. Only the claim's
  owner (same trusted agent, same hosted run) may send them, and a
  completed claim takes no more steps.

A claim is advisory. It tells others what an agent intends to do. It
never locks a target and never blocks anyone else's claim or write.

The server stamps every identity field from the checked
:class:`~meho_backplane.auth.agent_admission.AgentAdmission`. The caller
cannot set the owner, the actor, the run, or (inside a hosted run) the
work reference. A missing ``ttl_minutes`` becomes
:data:`~meho_backplane.broadcast.agent_events.TTL_DEFAULT_MINUTES`.

The database half of the rules (claim exists, owner matches, not yet
completed, idempotent replay) runs in
:func:`meho_backplane.broadcast.publisher.publish_agent_announcement`,
inside the same transaction as the insert.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID, uuid4

from meho_backplane.broadcast.agent_events import (
    TTL_DEFAULT_MINUTES,
    AgentAnnouncementEvent,
    PlannedOpClass,
)

if TYPE_CHECKING:
    # Type-only: the admission module imports this package's models.
    from meho_backplane.auth.agent_admission import AgentAdmission

__all__ = [
    "CLAIM_COMPLETED",
    "CLAIM_ID_NOT_ALLOWED",
    "CLAIM_ID_REQUIRED",
    "CLAIM_NOT_FOUND",
    "CLAIM_NOT_OWNED",
    "IDEMPOTENCY_KEY_REQUIRED",
    "IDEMPOTENCY_KEY_REUSED",
    "SERVER_STAMPED_FIELD",
    "AnnouncementClaimError",
    "build_trusted_announcement",
]

#: Stable machine codes carried on :class:`AnnouncementClaimError`.
CLAIM_ID_NOT_ALLOWED: Final[str] = "claim_id_not_allowed"
CLAIM_ID_REQUIRED: Final[str] = "claim_id_required"
IDEMPOTENCY_KEY_REQUIRED: Final[str] = "idempotency_key_required"
SERVER_STAMPED_FIELD: Final[str] = "server_stamped_field"
CLAIM_NOT_FOUND: Final[str] = "claim_not_found"
CLAIM_NOT_OWNED: Final[str] = "claim_not_owned"
CLAIM_COMPLETED: Final[str] = "claim_completed"
IDEMPOTENCY_KEY_REUSED: Final[str] = "idempotency_key_reused"

Phase = Literal["start", "update", "completion"]


class AnnouncementClaimError(Exception):
    """A claim step broke a rule. ``code`` is a stable machine code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _check_claim_step(phase: Phase, claim_id: UUID | None, idempotency_key: str | None) -> None:
    if phase == "start":
        if claim_id is not None:
            raise AnnouncementClaimError(
                CLAIM_ID_NOT_ALLOWED,
                "a 'start' opens a new claim and the server picks its claim_id",
            )
        return
    if claim_id is None:
        raise AnnouncementClaimError(
            CLAIM_ID_REQUIRED,
            f"an '{phase}' needs the claim_id returned by the claim's 'start'",
        )
    if idempotency_key is None:
        raise AnnouncementClaimError(
            IDEMPOTENCY_KEY_REQUIRED,
            f"an '{phase}' needs an idempotency_key so a retry is safe",
        )


def _check_stamped_fields(
    admission: AgentAdmission,
    run_id: UUID | None,
    work_ref: str | None,
) -> str | None:
    """Refuse caller values for server-stamped fields; return the work_ref to store.

    A ``run_id`` the caller sends is never identity proof. It is accepted
    only when it equals the checked run (old hosted clients still send
    it). Inside a hosted run the run row owns the work reference; outside
    one, a registered agent principal may declare its own.
    """
    if run_id is not None and run_id != admission.run_id:
        raise AnnouncementClaimError(
            SERVER_STAMPED_FIELD,
            "run_id is set by the server from the checked agent run; do not send it",
        )
    if admission.source != "hosted_run":
        return work_ref
    if work_ref is not None and work_ref != admission.work_ref:
        raise AnnouncementClaimError(
            SERVER_STAMPED_FIELD,
            "work_ref is set by the server from the hosted agent run; do not send it",
        )
    return admission.work_ref


def build_trusted_announcement(
    admission: AgentAdmission,
    *,
    tenant_id: UUID,
    activity: str,
    target: str | None,
    targets: list[str],
    scope: str | None,
    planned_op_class: PlannedOpClass | None,
    ttl_minutes: int | None,
    work_ref: str | None,
    run_id: UUID | None,
    phase: Phase,
    claim_id: UUID | None,
    idempotency_key: str | None,
    ts: datetime,
) -> AgentAnnouncementEvent:
    """Build the announcement event with every identity field stamped.

    Raises:
        AnnouncementClaimError: the step shape is wrong (missing or extra
            ``claim_id``, missing ``idempotency_key``) or the caller sent
            a value for a server-stamped field.
    """
    _check_claim_step(phase, claim_id, idempotency_key)
    stored_work_ref = _check_stamped_fields(admission, run_id, work_ref)
    return AgentAnnouncementEvent(
        tenant_id=tenant_id,
        principal_sub=admission.principal_sub,
        activity=activity,
        target=target,
        targets=targets,
        scope=scope,
        planned_op_class=planned_op_class,
        ttl_minutes=ttl_minutes if ttl_minutes is not None else TTL_DEFAULT_MINUTES,
        work_ref=stored_work_ref,
        run_id=admission.run_id,
        phase=phase,
        ts=ts,
        claim_id=uuid4() if phase == "start" else claim_id,
        owner_sub=admission.owner_sub,
        actor_sub=admission.actor_sub,
        admission=admission.source,
    )
