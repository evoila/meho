# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tick loop -- fire cron + one-off agent triggers (G11.3-T2 #823).

The lifespan-owned background ``asyncio`` task at the heart of the
scheduler. On each cadence (default 30s, settable via
``SCHEDULER_TICK_INTERVAL_SECONDS``):

1. **Claim the process-wide advisory lock**
   (``pg_try_advisory_lock``) so only one replica's loop is running the
   tick body at a time. Non-blocking: a replica that loses the race
   sleeps and tries the next cadence. Mirrors the
   :mod:`meho_backplane.topology.scheduler` precedent.

2. **Scan for due rows**
   (:func:`~meho_backplane.scheduler.repository.claim_due_triggers`)
   using ``SELECT ... FOR UPDATE SKIP LOCKED`` on PG so a hypothetical
   in-process double-claim still cannot deliver the same row to two
   coroutines.

3. **Fire each row**:

   * Cron: advance ``next_fire_at`` to the next cron match *before*
     invoking the agent. A slow agent run cannot delay the next tick.
   * One-off: transition ``status`` to ``fired`` *before* invoking the
     agent. The terminal write happens once; a double-claim under load
     finds the row already fired and skips it.

   The advance / mark-fired step is a conditional UPDATE
   (``WHERE status='active' AND next_fire_at=:previous``); a zero-row
   result means another claimer beat us to it and we skip the fire.

4. **Invoke the agent** through the G11.1-T4
   :class:`~meho_backplane.agent.invocation.AgentInvoker` in async mode
   so the scheduler tick returns promptly; the actual agent loop runs
   as a separate background task in the invoker's run store. The
   ``agent_run`` row's ``trigger`` column is set to
   ``AgentRunTrigger.SCHEDULED`` for provenance.

5. **Release the advisory lock** in a ``finally`` so a crash mid-tick
   never strands the lock for the rest of the connection's life.

Per-row failure isolation
=========================

Each row's fire runs inside its own ``try`` / ``except`` so one bad
trigger (corrupted cron expression, agent definition deleted, agent
disabled) never stalls the rest of the tick. The exception is logged
under ``scheduler_fire_failed`` with the row id; a corrupted cron
expression additionally transitions the row to ``paused`` so it stops
re-tripping the loop every tick.

Restart durability
==================

State lives in the DB row. On restart:

* A cron trigger whose ``next_fire_at`` has already passed (the pod
  was down through one or more scheduled instants) fires once on the
  next tick, advances to the next cron match, and resumes the normal
  cadence. No catch-up storm.
* A one-off trigger whose ``next_fire_at`` has already passed fires
  once on the next tick and transitions to ``fired``.

Replica-safety property
=======================

Two replicas running this loop against the same Postgres see exactly
one of them holding the advisory lock at any instant. The losing
replica sleeps a tick. Even if the advisory-lock claim were removed,
the ``SELECT ... FOR UPDATE SKIP LOCKED`` row claim plus the
conditional-UPDATE advance/mark-fired step guarantees single-fire
across all in-flight claimers.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.agent.invocation import (
    AgentDisabledError,
    AgentInvocationError,
    AgentInvoker,
    AgentNotFoundError,
    get_agent_invoker,
)
from meho_backplane.auth.agent_token import AgentTokenError
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentDefinition,
    ScheduledTrigger,
    ScheduledTriggerKind,
    ScheduledTriggerStatus,
)
from meho_backplane.scheduler.credentials import (
    AgentCredentialsUnresolvedError,
    resolve_agent_credentials,
)
from meho_backplane.scheduler.cron import InvalidCronExpressionError
from meho_backplane.scheduler.repository import (
    advance_cron_trigger,
    claim_due_triggers,
    mark_one_off_fired,
)
from meho_backplane.settings import get_settings

__all__ = [
    "run_one_tick",
    "start_scheduler",
    "stop_scheduler",
]

_log = structlog.get_logger(__name__)

#: 63-bit signed-int key for ``pg_try_advisory_lock``. A fixed literal
#: chosen at module-load time so every replica computes the same key
#: and exactly one of them can hold the lock at a time. The numeric
#: value is arbitrary but deliberately distinct from the topology
#: scheduler's per-target keyspace (which uses blake2b digests of
#: ``(tenant, target)`` UUIDs and lives in the same numeric range).
_SCHEDULER_ADVISORY_LOCK_KEY: int = 0x4D45_484F_5343_4844  # "MEHOSCHD"

#: Maximum rows the loop claims per tick. Bounds the per-tick work
#: even under a catch-up burst after a long outage; the next tick
#: picks up the remaining overdue rows. 50 is generous for cron +
#: one-off workloads (the consumer doc anticipates "dozens" of
#: triggers per deployment).
_CLAIM_BATCH_LIMIT: int = 50


@dataclass(frozen=True, slots=True)
class _ResolvedDefinition:
    """The agent-definition view the loop needs to invoke a fire.

    A minimal record so the per-tick DB work stays narrow: the full
    :class:`AgentDefinitionRead` is loaded by
    :meth:`AgentInvoker.run_scheduled` when it dispatches.

    * ``name`` -- the definition name the invoker calls by.
    * ``enabled`` -- gate that skips the fire without parking the row.
    * ``identity_ref`` -- the Keycloak client-id reference (e.g.
      ``agent:reporter``) the scheduler derives the
      ``client_credentials`` grant identity from. Fed into
      :func:`~meho_backplane.scheduler.credentials.resolve_agent_credentials`
      to source ``(client_id, client_secret)``.
    """

    name: str
    enabled: bool
    identity_ref: str


async def _try_advisory_lock(session: AsyncSession, key: int) -> bool:
    """Acquire the process-wide PG advisory lock; ``True`` on non-PG.

    Returns ``True`` when the lock is held (or the dialect has no
    advisory locks -- the SQLite single-replica test path) and the
    caller should proceed; ``False`` when another replica holds it
    and this tick is skipped.
    """
    conn = await session.connection()
    if conn.dialect.name != "postgresql":
        return True
    locked = await session.scalar(
        text("SELECT pg_try_advisory_lock(:k)"),
        {"k": key},
    )
    return bool(locked)


async def _advisory_unlock(session: AsyncSession, key: int) -> None:
    """Release the advisory lock; no-op on non-PG dialects."""
    conn = await session.connection()
    if conn.dialect.name != "postgresql":
        return
    await session.execute(
        text("SELECT pg_advisory_unlock(:k)"),
        {"k": key},
    )


async def _resolve_definition(
    session: AsyncSession,
    agent_definition_id: uuid.UUID | None,
    tenant_id: uuid.UUID,
) -> _ResolvedDefinition | None:
    """Look up the agent definition referenced by a trigger.

    Returns ``None`` when the row was deleted between trigger
    creation and the fire (the soft-FK relationship -- see the
    ``ScheduledTrigger`` docstring). A ``None`` resolution causes the
    loop to skip the fire and log; the trigger itself is left
    ``active`` so an operator who recreates the definition unblocks
    the schedule without re-creating the trigger.
    """
    if agent_definition_id is None:
        return None
    stmt = select(
        AgentDefinition.name,
        AgentDefinition.enabled,
        AgentDefinition.identity_ref,
    ).where(
        AgentDefinition.id == agent_definition_id,
        AgentDefinition.tenant_id == tenant_id,
    )
    result = await session.execute(stmt)
    row = result.first()
    if row is None:
        return None
    return _ResolvedDefinition(
        name=row.name,
        enabled=row.enabled,
        identity_ref=row.identity_ref,
    )


async def _park_trigger(
    session: AsyncSession,
    trigger_id: uuid.UUID,
    *,
    reason: str,
) -> None:
    """Transition a corrupted trigger to ``status='paused'``.

    Called for a row whose persisted ``cron_expr`` no longer parses;
    parking stops the loop from re-tripping on the same bad row every
    tick. The reason is logged so an operator following audit logs
    can find the offending row.
    """
    await session.execute(
        update(ScheduledTrigger)
        .where(ScheduledTrigger.id == trigger_id)
        .values(status=ScheduledTriggerStatus.PAUSED.value)
    )
    _log.warning(
        "scheduler_trigger_paused",
        trigger_id=str(trigger_id),
        reason=reason,
    )


def _coerce_inputs(inputs: dict[str, object]) -> str:
    """Render a trigger's ``inputs`` JSON into the str the invoker wants.

    The invocation surface's :meth:`AgentInvoker.run` takes ``inputs:
    str`` (the loop's user-prompt string). A scheduled trigger's
    payload is JSON-shaped for future extensibility, so the runtime
    contract is: prefer the conventional ``"prompt"`` key when present
    (the common shape), else dump the dict as JSON.
    """
    import json

    if "prompt" in inputs and isinstance(inputs["prompt"], str):
        return inputs["prompt"]
    return json.dumps(inputs, sort_keys=True, default=str)


async def _fire_cron(
    session: AsyncSession,
    row: ScheduledTrigger,
    invoker: AgentInvoker,
    *,
    fire_instant: datetime,
) -> bool:
    """Advance + fire one cron trigger; return ``True`` when the run was kicked off.

    The advance happens *before* the agent invocation so a slow agent
    run cannot delay the next tick. Returns ``False`` when the
    conditional advance lost a race to another claimer (the other
    replica owns this tick).
    """
    try:
        advanced = await advance_cron_trigger(
            session,
            row,
            fire_instant=fire_instant,
        )
    except InvalidCronExpressionError as exc:
        await _park_trigger(
            session,
            row.id,
            reason=f"invalid_cron_expr:{exc.expr!r}",
        )
        return False
    if advanced is None:
        return False
    await session.commit()
    return await _invoke_agent(session, row, invoker)


async def _fire_one_off(
    session: AsyncSession,
    row: ScheduledTrigger,
    invoker: AgentInvoker,
    *,
    fire_instant: datetime,
) -> bool:
    """Mark fired + invoke for one one-off trigger; ``True`` on launch.

    Same conditional-UPDATE discipline as the cron path: the mark-fired
    step is the single-fire enforcement, and a lost race returns
    ``False`` so the loop skips the agent invocation.
    """
    marked = await mark_one_off_fired(session, row, fire_instant=fire_instant)
    if marked is None:
        return False
    await session.commit()
    return await _invoke_agent(session, row, invoker)


async def _invoke_agent(
    session: AsyncSession,
    row: ScheduledTrigger,
    invoker: AgentInvoker,
) -> bool:
    """Resolve the trigger's definition + credentials and run the agent.

    Calls :meth:`AgentInvoker.run_scheduled` (G11.2-T2 #1096): the
    autonomous-agent path that obtains a Keycloak ``client_credentials``
    token, verifies the agent's principal binds the definition, and
    drives the run to completion under the agent's own identity (no
    delegating human actor; ``actor_sub`` stays NULL on the audit
    rows). The :attr:`AgentRunTrigger.SCHEDULED` provenance is set
    inside :meth:`run_scheduled` so a cron / one-off fire is
    distinguishable from a direct invocation in audit queries.

    Credential sourcing -- the new credential boundary T2 introduces --
    runs through :func:`resolve_agent_credentials` against
    :attr:`Settings.scheduler_agent_secret_env_pattern`. An
    :class:`AgentCredentialsUnresolvedError` is logged + skipped (the
    trigger stays ``active`` so an operator who wires the secret
    unblocks the schedule on the next tick); the row is not parked,
    matching the soft-FK-missing-definition recovery shape.

    A re-resolution session is opened (not the claim session) because
    the claim session's transaction is committed at this point and
    holds no locks; using a fresh session keeps the lookup query off
    the claim's connection.

    *session* is unused here but kept on the signature so
    :func:`_fire_cron` / :func:`_fire_one_off` callers do not need a
    second wrapper.
    """
    del session  # The lookup uses its own session; see docstring.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as lookup_session:
        definition = await _resolve_definition(
            lookup_session,
            row.agent_definition_id,
            row.tenant_id,
        )
    if definition is None:
        _log.warning(
            "scheduler_definition_missing",
            trigger_id=str(row.id),
            agent_definition_id=(
                str(row.agent_definition_id) if row.agent_definition_id is not None else None
            ),
        )
        return False
    if not definition.enabled:
        _log.info(
            "scheduler_definition_disabled",
            trigger_id=str(row.id),
            agent_name=definition.name,
        )
        return False
    try:
        agent_client_id, agent_client_secret = resolve_agent_credentials(
            definition.identity_ref,
        )
    except AgentCredentialsUnresolvedError as exc:
        # Operator must wire the agent secret; leave the trigger
        # ``active`` so the next tick retries automatically once the
        # secret is present.
        _log.warning(
            "scheduler_credentials_unresolved",
            trigger_id=str(row.id),
            agent_name=definition.name,
            identity_ref=definition.identity_ref,
            reason=str(exc),
        )
        return False
    inputs_str = _coerce_inputs(row.inputs)
    try:
        outcome = await invoker.run_scheduled(
            definition.name,
            inputs_str,
            agent_client_id=agent_client_id,
            agent_client_secret=agent_client_secret,
        )
    except (AgentNotFoundError, AgentDisabledError, AgentInvocationError) as exc:
        # AgentInvocationError covers the identity-binding refusal
        # (the agent's credentials don't own the definition name) the
        # scheduler must not retry blindly -- a misconfigured trigger
        # would otherwise log-spam every tick.
        _log.warning(
            "scheduler_invoke_refused",
            trigger_id=str(row.id),
            agent_name=definition.name,
            reason=type(exc).__name__,
        )
        return False
    except AgentTokenError as exc:
        # Network / Keycloak failure on the client_credentials grant.
        # Transient by nature; the next tick retries. Logged at WARN
        # so monitoring can alert on sustained failures without
        # parking the trigger.
        _log.warning(
            "scheduler_token_grant_failed",
            trigger_id=str(row.id),
            agent_name=definition.name,
            reason=type(exc).__name__,
        )
        return False
    _log.info(
        "scheduler_fired",
        trigger_id=str(row.id),
        kind=row.kind,
        agent_name=definition.name,
        agent_run_id=str(outcome.run_id),
    )
    return True


async def run_one_tick(invoker: AgentInvoker | None = None) -> int:
    """Execute one scheduler tick. Returns the number of agents fired.

    Public so tests can drive a deterministic single-tick without the
    cadence sleep. The optional *invoker* override lets tests inject a
    deterministic :class:`AgentInvoker` over a ``FunctionModel`` so the
    fire path executes end-to-end without a real LLM call.
    """
    if invoker is None:
        invoker = get_agent_invoker()
    sessionmaker = get_sessionmaker()
    fires = 0
    async with sessionmaker() as session:
        locked = await _try_advisory_lock(session, _SCHEDULER_ADVISORY_LOCK_KEY)
        if not locked:
            return 0
        try:
            now = datetime.now(UTC)
            rows = await claim_due_triggers(
                session,
                now=now,
                limit=_CLAIM_BATCH_LIMIT,
            )
            for row in rows:
                try:
                    if row.kind == ScheduledTriggerKind.CRON.value:
                        fired = await _fire_cron(
                            session,
                            row,
                            invoker,
                            fire_instant=now,
                        )
                    elif row.kind == ScheduledTriggerKind.ONE_OFF.value:
                        fired = await _fire_one_off(
                            session,
                            row,
                            invoker,
                            fire_instant=now,
                        )
                    else:
                        # An unrecognised kind is a corrupt row -- park.
                        await _park_trigger(
                            session,
                            row.id,
                            reason=f"unknown_kind:{row.kind!r}",
                        )
                        await session.commit()
                        continue
                    if fired:
                        fires += 1
                except Exception:
                    # Per-row isolation: one bad row never stalls the
                    # tick. The row remains active (next tick retries),
                    # except for the explicit park paths above.
                    _log.exception(
                        "scheduler_fire_failed",
                        trigger_id=str(row.id),
                    )
                    # Roll back any partial work on this row so the
                    # next row's claim sees a clean session.
                    await session.rollback()
        finally:
            await _advisory_unlock(session, _SCHEDULER_ADVISORY_LOCK_KEY)
            # Commit the unlock (PG sessions hold no transaction across
            # the advisory-unlock call but flush + commit is the
            # mirror-image of the topology scheduler's discipline).
            await session.commit()
    return fires


async def _scheduler_loop() -> None:
    """The forever loop: sleep one cadence, tick, repeat.

    Sleep-then-tick (rather than tick-then-sleep) so the first tick
    after process start is delayed by one cadence -- letting the rest
    of the lifespan eager-init complete before the loop touches the DB.
    Per-tick ``try`` / ``except`` so a transient failure (DB blip,
    advisory-lock query error) is logged and the loop continues.
    """
    interval = get_settings().scheduler_tick_interval_seconds
    _log.info("scheduler_started", interval_seconds=interval)
    while True:
        # Sleep first so the very first tick does not race the rest of
        # the lifespan startup; CancelledError here unwinds cleanly.
        await asyncio.sleep(get_settings().scheduler_tick_interval_seconds)
        try:
            await run_one_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning("scheduler_tick_failed", exc_info=True)


def start_scheduler() -> asyncio.Task[None]:
    """Start the background scheduler loop and return its task handle.

    Registered in :func:`meho_backplane.main.lifespan` behind the
    ``SCHEDULER_ENABLED`` setting. The returned task is cancelled on
    lifespan shutdown; the caller awaits the cancellation so the loop
    unwinds cleanly. Returning the task (rather than fire-and-forget)
    keeps a strong reference alive -- an un-referenced
    :class:`asyncio.Task` can be GC'd mid-flight, producing the "Task
    was destroyed but it is pending!" warnings pytest-asyncio shutdown
    fails on.
    """
    return asyncio.create_task(_scheduler_loop(), name="scheduler-loop")


async def stop_scheduler(task: asyncio.Task[None]) -> None:
    """Cancel the scheduler task and await its unwind.

    Swallows the expected :class:`asyncio.CancelledError`; any other
    exception during unwind propagates so a broken shutdown is visible
    rather than silently swallowed. Mirrors
    :func:`~meho_backplane.topology.scheduler.stop_topology_refresh_scheduler`
    verbatim so future contributors find one disposal pattern across
    every lifespan-owned task.
    """
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
