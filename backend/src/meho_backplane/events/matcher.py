# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Subscription matcher -- fire ``kind=event`` triggers off drained events (G11.3).

The drain loop (:mod:`meho_backplane.events.drain`) claims unprocessed
``event_outbox`` rows; for each one it calls :func:`fire_matching_triggers`
here. The matcher looks up the tenant's active ``kind=event``
:class:`~meho_backplane.db.models.ScheduledTrigger` rows, keeps the ones
whose ``event_filter`` is *contained by* the event ``payload``, and fires
each through the same
:meth:`~meho_backplane.agent.invocation.AgentInvoker.run_scheduled` seam the
cron / one-off scheduler loop uses.

Match semantics -- ``payload @> event_filter``
==============================================

An event matches a trigger when the event ``payload`` **contains** every
key/value the trigger's ``event_filter`` names -- the Postgres jsonb ``@>``
direction (the retrieval-metadata-filter precedent,
:mod:`meho_backplane.retrieval.retriever`). A filter that is a subset of the
payload matches; a payload that is a subset of the filter does **not**
(unless they are equal). An empty filter (``{}``) contains no constraints and
so matches every event of the tenant.

The containment check is a single pure-Python predicate
(:func:`_payload_contains`) used on **both** dialects rather than the native
Postgres ``@>`` on PG and a portable fallback on SQLite. One code path is a
deliberate choice: the drain's tests run on SQLite, so a single predicate
means those tests exercise the exact code that also runs on Postgres --
there is no second implementation that can silently diverge from the first,
which is what the "works on PG and SQLite" contract needs. There is no GIN
index on ``scheduled_trigger.event_filter``, so a native ``@>`` would give no
index-backed speed-up over loading the tenant's (dozens of) event triggers
and filtering them in Python, which the drain already does row-by-row.

Firing -- reuse of the scheduler fire recipe
============================================

Resolving the agent definition + credentials, holding the client secret as a
:class:`~pydantic.SecretStr` end-to-end (CWE-532), and treating
:class:`~meho_backplane.agent.invocation.BudgetExceededError` as a
do-not-retry single-log refusal are all identical to the cron / one-off fire
path, so the matcher reuses
:func:`~meho_backplane.scheduler.loop._prepare_invocation` and
:func:`~meho_backplane.scheduler.loop._dispatch_invocation` verbatim. Only two
values differ for an event fire and are overridden on the prepared invocation:

* **inputs** -- ``kind=event`` triggers are exempt from the create-time
  non-empty-inputs rule (:mod:`meho_backplane.scheduler.schemas`), so an
  input-less event trigger reaches here with no operator prompt. Rather than
  let the fire fail typed at the no-input guard
  (:meth:`AgentInvoker._launch_scheduled_run`), the matcher synthesises a
  prompt from the matched event. Event bodies are untrusted, so the composed
  text is wrapped with
  :func:`~meho_backplane.untrusted_text.wrap_untrusted_text`.
* **work_ref** -- the fired run's work_ref is ``event:{event_id}:{trigger_id}``,
  the fire-dedupe key. It rides the existing ``agent_run_tenant_work_ref_idx``
  (the checks-investigator precedent) so a **redelivered** event -- one whose
  fire committed but whose ``processed_at`` stamp did not, then re-claimed on a
  later tick -- does not double-fire: :func:`_run_exists_for_work_ref` finds
  the run from the first delivery and the matcher skips it. The run inherits
  the work_ref through the trigger->run ``work_ref_var`` inheritance seam
  ``run_scheduled`` already plumbs.

Delivery is at-least-once (fire, then the drain stamps ``processed_at`` in a
separate transaction); the work_ref dedupe is what makes a duplicate delivery
idempotent. A single drain replica runs at a time (the drain advisory lock),
so the check-then-fire dedupe never races itself in production.
"""

from __future__ import annotations

import json
from dataclasses import replace

import structlog
from sqlalchemy import select

from meho_backplane.agent.invocation import AgentInvoker
from meho_backplane.agent.run import prompt_is_effectively_empty
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentRun,
    EventOutbox,
    ScheduledTrigger,
    ScheduledTriggerKind,
    ScheduledTriggerStatus,
)
from meho_backplane.operations.agent_run import AGENT_RUN_COMPLETED_EVENT_KIND
from meho_backplane.scheduler.loop import (
    _coerce_inputs,
    _dispatch_invocation,
    _PreconditionSkip,
    _prepare_invocation,
)
from meho_backplane.untrusted_text import wrap_untrusted_text

__all__ = ["fire_matching_triggers"]

_log = structlog.get_logger(__name__)

#: work_ref prefix for an event-fired run. The full ref is
#: ``event:{event_id}:{trigger_id}`` -- unique per (event, trigger) pair, so it
#: doubles as the fire-dedupe key on ``agent_run_tenant_work_ref_idx``.
_EVENT_WORK_REF_PREFIX = "event:"

#: The ``_coerce_inputs`` render of an empty ``inputs`` dict. ``{}`` is
#: non-whitespace, so ``prompt_is_effectively_empty`` reads it as a real prompt;
#: an event trigger created with ``inputs: {}`` must still synthesise a prompt,
#: so it is treated as no-prompt here (matching the create-time
#: ``_payload_yields_prompt`` tightening).
_EMPTY_DICT_RENDER = "{}"


def _payload_contains(container: object, subset: object) -> bool:
    """Return ``True`` when *container* contains *subset* (jsonb ``@>`` semantics).

    A portable **approximation** of the Postgres ``container @> subset``
    operator for the payload shapes we match (``payload @> event_filter``):

    * object contains object -- every key of *subset* is present in *container*
      and its value is (recursively) contained;
    * array contains array -- every element of *subset* is contained in some
      element of *container* (order-independent subset);
    * scalar contains scalar -- equality.

    An empty object subset is contained by any object (vacuously true), so an
    empty ``event_filter`` matches every payload.

    Two edges of the real ``@>`` are deliberately not modelled -- neither is
    reachable with the ``agent_run.completed`` payloads and operator-authored
    filters we match, and one predicate shared by both dialects is worth more
    than bit-exact parity: PG treats a top-level array as containing a bare
    scalar it holds (``'[1,2]'::jsonb @> '2'``), which the array branch below
    does not; and Python conflates ``True == 1`` / ``False == 0`` where PG's
    jsonb keeps ``true`` and ``1`` distinct.
    """
    if isinstance(subset, dict):
        return isinstance(container, dict) and all(
            key in container and _payload_contains(container[key], value)
            for key, value in subset.items()
        )
    if isinstance(subset, list):
        return isinstance(container, list) and all(
            any(_payload_contains(item, sub_element) for item in container)
            for sub_element in subset
        )
    return container == subset


async def _matching_event_triggers(event: EventOutbox) -> list[ScheduledTrigger]:
    """Return the tenant's active event triggers whose filter the payload contains.

    A fresh session (separate from the drain's claim transaction) reads the
    candidate rows -- ``kind=event`` triggers have ``next_fire_at IS NULL`` so
    they are invisible to the scheduler's due-row scan; this is the only query
    that wakes them. Containment is applied in Python (see the module
    docstring). Trigger corpora are "dozens per tenant" per the consumer doc,
    so the per-event candidate load stays small.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(ScheduledTrigger).where(
                ScheduledTrigger.kind == ScheduledTriggerKind.EVENT.value,
                ScheduledTrigger.status == ScheduledTriggerStatus.ACTIVE.value,
                ScheduledTrigger.tenant_id == event.tenant_id,
            )
        )
        triggers = list(result.scalars().all())
    return [t for t in triggers if _payload_contains(event.payload, t.event_filter or {})]


async def _run_exists_for_work_ref(tenant_id: object, work_ref: str) -> bool:
    """Return ``True`` iff any run already carries this (tenant, work_ref).

    The fire-dedupe read. Unlike the checks-investigator's in-flight check this
    matches a run in **any** status: ``event:{event_id}:{trigger_id}`` is unique
    per delivery, so a run in that work_ref -- terminal or not -- means the
    (event, trigger) pair already fired and a redelivery must not fire it again.
    Rides ``agent_run_tenant_work_ref_idx`` on ``(tenant_id, work_ref)``.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(AgentRun.id)
            .where(
                AgentRun.tenant_id == tenant_id,
                AgentRun.work_ref == work_ref,
            )
            .limit(1)
        )
        return result.first() is not None


def _synthesize_event_prompt(event: EventOutbox) -> str:
    """Compose a prompt describing *event*, wrapped in the untrusted-text guard.

    Used for an input-less event trigger. The event kind + payload are
    agent-authored / externally-sourced untrusted data, so the composed body
    is wrapped with :func:`wrap_untrusted_text` -- the reading agent sees it as
    data to act on, not a directive channel.
    """
    body = json.dumps(
        {"event_kind": event.event_kind, "payload": event.payload},
        sort_keys=True,
        indent=2,
        default=str,
    )
    return (
        "A subscribed MEHO event matched this trigger's filter and started this "
        "run. The event that fired it is described below; decide what to do "
        "based on it.\n\n" + wrap_untrusted_text(body)
    )


def _event_inputs(trigger: ScheduledTrigger, event: EventOutbox) -> str:
    """Render the fire-time prompt string for an event trigger.

    Prefers the operator-supplied ``inputs`` when it renders a usable prompt
    (the ``"prompt"`` key or a non-empty structured payload, via
    :func:`_coerce_inputs`); otherwise synthesises one from the matched event
    so an input-less event trigger still fires with a real user turn instead of
    failing typed at the no-input guard.
    """
    coerced = _coerce_inputs(trigger.inputs)
    if coerced and coerced != _EMPTY_DICT_RENDER and not prompt_is_effectively_empty(coerced):
        return coerced
    return _synthesize_event_prompt(event)


def _is_self_trigger_completion(event: EventOutbox, trigger: ScheduledTrigger) -> bool:
    """Return ``True`` when firing *trigger* off *event* is a direct self-loop.

    A ``kind=event`` trigger that spawns agent X, subscribed with a filter broad
    enough to match X's own ``agent_run.completed`` event, would re-fire itself
    on every completion -- and on a default install with no budget row that loop
    is bounded only by the manual kill switch. This guard breaks the *direct*
    one-hop loop with no schema change: it suppresses the fire when the draining
    event is the completion of the very agent this trigger spawns
    (``event_kind == agent_run.completed`` and the event payload's
    ``agent_definition_id`` is this trigger's target). A cross-agent chain -- X's
    completion firing a trigger that spawns a *different* definition Y -- is
    unaffected, since the ids differ. A longer cycle (X -> Y -> X) is still the
    subscription filter's job to avoid (see the module docstring).
    """
    if event.event_kind != AGENT_RUN_COMPLETED_EVENT_KIND:
        return False
    return event.payload.get("agent_definition_id") == str(trigger.agent_definition_id)


async def _fire_event_trigger(
    trigger: ScheduledTrigger,
    event: EventOutbox,
    invoker: AgentInvoker,
) -> bool:
    """Fire one matched event trigger; return ``True`` when a run was started.

    Suppresses a direct self-trigger loop (:func:`_is_self_trigger_completion`),
    resolves definition + credentials via the scheduler's precondition gate,
    dedupes on the ``event:{event_id}:{trigger_id}`` work_ref, then dispatches
    through the shared fire seam with the event prompt + dedupe work_ref
    overridden onto the prepared invocation.
    """
    if _is_self_trigger_completion(event, trigger):
        _log.info(
            "event_trigger_self_fire_suppressed",
            event_id=event.event_id,
            trigger_id=str(trigger.id),
            agent_definition_id=str(trigger.agent_definition_id),
        )
        return False
    prepared = await _prepare_invocation(trigger)
    if isinstance(prepared, _PreconditionSkip):
        # _prepare_invocation already logged the cause (definition
        # missing/disabled, credentials unresolved). Unlike the scheduler loop
        # there is nothing to advance or park: an event trigger carries no
        # next_fire_at, so a transient precondition miss simply means this
        # event does not fire it and a later matching event re-attempts once
        # the operator fixes the cause.
        return False
    work_ref = f"{_EVENT_WORK_REF_PREFIX}{event.event_id}:{trigger.id}"
    if await _run_exists_for_work_ref(trigger.tenant_id, work_ref):
        _log.info(
            "event_trigger_deduped",
            event_id=event.event_id,
            trigger_id=str(trigger.id),
            work_ref=work_ref,
        )
        return False
    prepared = replace(
        prepared,
        inputs_str=_event_inputs(trigger, event),
        work_ref=work_ref,
    )
    return await _dispatch_invocation(trigger, prepared, invoker)


async def fire_matching_triggers(event: EventOutbox, invoker: AgentInvoker) -> int:
    """Fire every active event trigger whose filter the *event* payload contains.

    Returns the number of triggers that started a run. Per-trigger failures are
    isolated (one bad subscription never stalls the others or the drain tick);
    a budget refusal / precondition skip / dedupe hit returns ``False`` for
    that trigger without raising.
    """
    triggers = await _matching_event_triggers(event)
    if not triggers:
        return 0
    fired = 0
    for trigger in triggers:
        try:
            if await _fire_event_trigger(trigger, event, invoker):
                fired += 1
        except Exception:
            _log.exception(
                "event_trigger_fire_failed",
                event_id=event.event_id,
                trigger_id=str(trigger.id),
            )
    if fired:
        _log.info(
            "event_subscriptions_fired",
            event_id=event.event_id,
            event_kind=event.event_kind,
            matched=len(triggers),
            fired=fired,
        )
    return fired
