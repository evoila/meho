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
   :class:`~meho_backplane.agent.invocation.AgentInvoker` via
   :meth:`~meho_backplane.agent.invocation.AgentInvoker.run_scheduled`.
   The actual agent loop runs as a background task in the invoker's run
   store; ``run_scheduled`` waits on it **bounded** by
   ``AGENT_SYNC_TIMEOUT_SECONDS`` (default 30s) and, on timeout, returns
   the still-running handle (``converted_to_async``) while the loop keeps
   going in the background. This bound is what keeps the serial tick
   returning promptly — and the advisory lock released each tick — even
   when a run hangs or blocks on a ``requires_approval`` wait (#1502).
   The ``agent_run`` row's ``trigger`` column is set to
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

Delivery semantics
==================

The dispatcher is **at-most-once** per scheduled instant, *not*
exactly-once. The advance/mark-fired conditional UPDATE commits
*before* the actual agent invoke so an invoke that crashes / times
out leaves the trigger advanced (cron) or terminal (one-off) with no
``agent_run`` row recorded. This is the conservative direction the
consumer doc (G11.3-T4) accepts: a missed fire is visible in audit
(the advance/fired transition is logged) and the operator can
manually re-fire via the admin surface. The opposite choice (commit
*after* invoke) would risk double-fire under crash-during-commit and
is rejected for that reason.

Precondition gate vs. invoke-time failure
-----------------------------------------

The at-most-once contract applies to *invoke-time* failures only --
the agent loop crashing, the Keycloak grant timing out, the JWT
verifier rejecting the issued token. Failures of *precondition*
state -- the agent definition was deleted, the agent is disabled,
the agent's secret hasn't been wired into the pod env yet --
short-circuit through :func:`_prepare_invocation` **before** the
advance/mark-fired step, so the row's scheduled instant is **not**
consumed: a subsequent tick re-runs the precondition gate and either
fires (operator fixed the underlying issue) or short-circuits again.
This split keeps the at-most-once contract honest for the cases it
was designed for (invoker crash) without silently dropping one-off
work for the cases it was not (missing config that the operator can
fix without re-creating the trigger).

Operators wanting at-least-once semantics on a per-trigger basis set
``in_flight_policy = 'resume'`` (T4 #825 owns the resume mechanics).
The default ``fail_into_audit`` keeps the at-most-once contract.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from pydantic import SecretStr
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.agent.invocation import (
    AgentDisabledError,
    AgentInvocationError,
    AgentInvoker,
    AgentNotFoundError,
    BudgetExceededError,
    get_agent_invoker,
)
from meho_backplane.auth.agent_token import AgentTokenError
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentDefinition,
    AgentRunStatus,
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
    agent_definition_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> _ResolvedDefinition | None:
    """Look up the agent definition referenced by a trigger.

    Returns ``None`` only when the FK lookup yields no row -- the
    ``agent_definition`` table is the real-FK parent (migration 0020
    tightened it from a soft reference), so the only way to hit
    ``None`` is the parent row being removed after the trigger was
    created and before the fire. A ``None`` resolution causes the
    loop to skip the fire and log; the trigger itself is left
    ``active`` so an operator who recreates the definition unblocks
    the schedule without re-creating the trigger.
    """
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


def _coerce_inputs(inputs: dict[str, object] | None) -> str:
    """Render a trigger's ``inputs`` JSON into the str the invoker wants.

    The invocation surface's :meth:`AgentInvoker.run` takes ``inputs:
    str`` (the loop's user-prompt string). A scheduled trigger's
    payload is JSON-shaped for future extensibility, so the runtime
    contract is: prefer the conventional ``"prompt"`` key when present
    (the common shape), else dump the dict as JSON. ``None`` (a trigger
    created without ``inputs``) renders as an empty string.

    An empty string is **not** a valid user turn: every supported model
    backend drops a whitespace-only user prompt, leaving an empty
    ``messages`` array that the provider 400s on (the system prompt rides
    the separate ``system`` param and does not count). Rather than
    inject a synthetic user turn here (which would misrepresent operator
    intent), the empty result is caught downstream by the typed no-input
    guard in :meth:`AgentInvoker._launch_scheduled_run`, which finalises
    the run ``failed`` with a
    :data:`~meho_backplane.agent.run.SCHEDULED_RUN_NO_INPUT_CLASS` tag
    before any model call (#1505).
    """
    if inputs is None:
        return ""
    if "prompt" in inputs and isinstance(inputs["prompt"], str):
        return inputs["prompt"]
    return json.dumps(inputs, sort_keys=True, default=str)


@dataclass(frozen=True, slots=True)
class _PreparedInvocation:
    """Precondition snapshot for a fire: definition + credentials + inputs.

    Built by :func:`_prepare_invocation` *before* the advance / mark-
    fired commit, so a failure on any of these precondition lookups
    (missing definition, disabled agent, unresolved credentials)
    leaves the trigger's row state untouched and a subsequent tick
    can re-try cleanly once the operator fixes the underlying issue.
    Once a :class:`_PreparedInvocation` is in hand the caller may
    advance/mark-fire the row and dispatch the run; the at-most-once
    contract still applies *after* the conditional UPDATE commits
    (invoker exceptions are logged but the row is not re-fired).
    """

    name: str
    identity_ref: str
    agent_client_id: str
    #: The agent's ``client_credentials`` secret, held as a
    #: :class:`~pydantic.SecretStr` so it can never be rendered into a log
    #: line. A failed scheduled fire is logged via ``_log.exception`` on
    #: ``run_one_tick``'s broad ``except`` (:func:`run_one_tick`), and the
    #: structlog ``dict_tracebacks`` processor renders frame locals
    #: (``show_locals``) -- a plain ``str`` here would print the secret
    #: verbatim into stdout (CWE-532). ``SecretStr`` masks to
    #: ``'**********'`` even as a bare frame local; the real value is read
    #: only at the token-mint call site via ``.get_secret_value()``.
    agent_client_secret: SecretStr
    inputs_str: str
    #: The firing trigger's external change-ticket reference
    #: (work_ref I3-T3 #1663), copied off ``row.work_ref`` at prepare
    #: time. The dispatcher binds ``work_ref_var`` from this value around
    #: :meth:`AgentInvoker.run_scheduled` so the dispatched run's
    #: ``agent_run.work_ref`` and every audit row the run produces inherit
    #: the trigger's ref end-to-end. ``None`` when the trigger carries no
    #: change ticket. This is the seam the Initiative #1654 widens: today
    #: the dispatch carried only name + inputs, so a dispatched run could
    #: not inherit the trigger's ref.
    work_ref: str | None


async def _prepare_invocation(row: ScheduledTrigger) -> _PreparedInvocation | None:
    """Resolve definition + credentials for a due trigger; ``None`` to skip.

    Called *before* the advance/mark-fired commit so a precondition
    miss (definition deleted, agent disabled, agent secret not wired)
    does not consume the trigger's scheduled instant -- the row stays
    ``active`` with its current ``next_fire_at``/``fire_at`` so the
    next tick retries.

    Returns:
        :class:`_PreparedInvocation` on success.
        ``None`` when the caller should skip the fire without advancing.

    Skip-with-``None`` cases (each logged at WARN/INFO before return):

    * **definition missing** -- the FK lookup returned no row. The
      ``agent_definition`` row was removed after the trigger was
      created; an operator who recreates the definition unblocks the
      schedule on the next tick.
    * **definition disabled** -- the agent definition exists but
      ``enabled=False``. Same recovery shape: flip the flag, schedule
      resumes.
    * **credentials unresolved** -- neither the Vault path
      (:attr:`Settings.scheduler_agent_vault_path_pattern`, read under
      ``VAULT_SCHEDULER_TOKEN``) nor the fallback env var derived from
      :attr:`Settings.scheduler_agent_secret_env_pattern` yields a
      secret. Registering the agent over the API persists the secret to
      Vault; the next tick retries.

    The lookup session is opened fresh -- separate from the claim
    session whose transaction is still open at the caller -- so the
    SELECT keeps off the claim's connection.
    """
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
            agent_definition_id=str(row.agent_definition_id),
        )
        return None
    if not definition.enabled:
        _log.info(
            "scheduler_definition_disabled",
            trigger_id=str(row.id),
            agent_name=definition.name,
        )
        return None
    try:
        agent_client_id, agent_client_secret = await resolve_agent_credentials(
            definition.identity_ref,
        )
    except AgentCredentialsUnresolvedError as exc:
        _log.warning(
            "scheduler_credentials_unresolved",
            trigger_id=str(row.id),
            agent_name=definition.name,
            identity_ref=definition.identity_ref,
            reason=str(exc),
        )
        return None
    return _PreparedInvocation(
        name=definition.name,
        identity_ref=definition.identity_ref,
        agent_client_id=agent_client_id,
        # Wrap the resolved secret immediately so it lives as a SecretStr
        # for the rest of its lifetime -- the plain ``agent_client_secret``
        # local above is the only frame that holds it bare, and it is not
        # on the failure-logging traceback (this function returns before
        # any fire that ``run_one_tick`` would log on).
        agent_client_secret=SecretStr(agent_client_secret),
        inputs_str=_coerce_inputs(row.inputs),
        # work_ref I3-T3 #1663: snapshot the trigger's change-ticket ref so
        # the dispatcher can bind it onto the run for inheritance.
        work_ref=row.work_ref,
    )


async def _fire_cron(
    session: AsyncSession,
    row: ScheduledTrigger,
    invoker: AgentInvoker,
    *,
    fire_instant: datetime,
) -> bool:
    """Advance + fire one cron trigger; return ``True`` when the run was kicked off.

    Lifecycle:

    1. **Prepare** -- look up the definition + credentials BEFORE any
       state write. A precondition miss returns ``False`` with the
       trigger row untouched, so the next tick re-tries (no missed
       cron instant attributed to a misconfigured side-channel).
    2. **Advance** -- ``advance_cron_trigger`` commits the next
       ``next_fire_at`` *before* the agent invocation. A slow agent
       run cannot delay the next tick.
    3. **Dispatch** -- call the invoker. Invocation-time failures
       (token grant, identity binding) follow the at-most-once
       contract: the advance has already committed; the missed
       instant is visible in audit and the trigger continues firing
       on subsequent cron matches.

    Returns ``False`` when the precondition gate skipped (Step 1)
    or the conditional advance lost a race to another claimer (the
    other replica owns this tick).
    """
    prepared = await _prepare_invocation(row)
    if prepared is None:
        return False
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
        # Commit the park UPDATE so a sibling-row rollback later in
        # ``run_one_tick`` cannot undo it. Without this commit the
        # session-level rollback for a sibling row's failure would
        # revert the park and leave the corrupted-cron row re-tripping
        # this handler on every tick (CPU + log-spam, no recovery).
        # Mirrors the post-park commit in the ``unknown_kind`` path
        # elsewhere in the loop -- the per-row failure-isolation
        # discipline the module docstring documents.
        await session.commit()
        return False
    if advanced is None:
        return False
    await session.commit()
    return await _dispatch_invocation(row, prepared, invoker)


async def _fire_one_off(
    session: AsyncSession,
    row: ScheduledTrigger,
    invoker: AgentInvoker,
    *,
    fire_instant: datetime,
) -> bool:
    """Mark fired + invoke for one one-off trigger; ``True`` on launch.

    Same lifecycle shape as :func:`_fire_cron`: precondition gate
    runs *before* :func:`mark_one_off_fired` so a missing-definition
    / disabled / unresolved-credentials case does **not** consume the
    one-off. The row stays ``status='active'`` so a subsequent tick
    retries once the operator fixes the underlying issue. Without
    this gate the at-most-once contract would silently drop one-off
    work on every credential-rotation gap, with no admin re-fire path
    in v0.2 (T5 #826 unbuilt).

    After mark-fired commits, invocation-time failures follow the
    at-most-once contract -- the row is terminal and a missed fire
    is visible in audit.
    """
    prepared = await _prepare_invocation(row)
    if prepared is None:
        return False
    marked = await mark_one_off_fired(session, row, fire_instant=fire_instant)
    if marked is None:
        return False
    await session.commit()
    return await _dispatch_invocation(row, prepared, invoker)


# _dispatch_invocation is a dispatch handler with four exception branches + two
# outcome branches, each carrying a load-bearing at-most-once-contract comment;
# splitting fragments that single error-contract. It was already at the 100-line
# limit before #1663 added the one-line ``work_ref=`` forward.
# code-quality-allow: function-size — irreducible error-contract dispatcher (see above)
async def _dispatch_invocation(
    row: ScheduledTrigger,
    prepared: _PreparedInvocation,
    invoker: AgentInvoker,
) -> bool:
    """Call :meth:`AgentInvoker.run_scheduled` for a prepared fire.

    Calls G11.2-T2's autonomous-agent seam (G11.2-T2 #1096): the
    invoker obtains a Keycloak ``client_credentials`` token using
    *prepared*'s ``(agent_client_id, agent_client_secret)``, verifies
    the JWT, asserts the agent's principal owns the definition by
    name, and drives the run to completion under the agent's own
    identity (``actor_sub`` stays NULL on the audit rows). The
    :attr:`AgentRunTrigger.SCHEDULED` provenance is set inside
    :meth:`run_scheduled` so a cron / one-off fire is distinguishable
    from a direct invocation in audit queries.

    work_ref I3-T3 (#1663): *prepared*'s ``work_ref`` is forwarded into
    :meth:`run_scheduled`, which binds ``work_ref_var`` so the dispatched
    run + its audit rows inherit the trigger's ref -- the seam that
    before #1663 carried only name + inputs.

    Errors are logged + swallowed (return ``False``); the at-most-once
    contract documented in the module docstring applies -- the
    advance/mark-fired commit has already happened, so a transient
    grant failure or a misconfigured identity binding does not
    re-fire.
    """
    try:
        outcome = await invoker.run_scheduled(
            prepared.name,
            prepared.inputs_str,
            agent_client_id=prepared.agent_client_id,
            agent_client_secret=prepared.agent_client_secret,
            work_ref=prepared.work_ref,
        )
    except (
        AgentNotFoundError,
        AgentDisabledError,
        AgentInvocationError,
        BudgetExceededError,
    ) as exc:
        # AgentInvocationError covers the identity-binding refusal
        # (the agent's credentials don't own the definition name) the
        # scheduler must not retry blindly -- a misconfigured trigger
        # would otherwise log-spam every tick. BudgetExceededError
        # (G11.5-T6 #1080) is the per-identity / per-tenant / global
        # pre-execution budget refusal; treated the same way -- the
        # scheduler must not blast through a kill switch on every
        # tick, the cap is the contract. When the refusal is a budget
        # gate, also surface ``exc.reason`` (machine-readable refusal
        # tag: ``per_identity_*``, ``per_tenant_kill_switch``,
        # ``global_kill_switch``) so on-call can tell from a single
        # log line which gate fired, without grepping for the
        # exception's text.
        log_kwargs = {
            "trigger_id": str(row.id),
            "agent_name": prepared.name,
            "reason": type(exc).__name__,
        }
        if isinstance(exc, BudgetExceededError):
            log_kwargs["budget_reason"] = exc.reason
        _log.warning("scheduler_invoke_refused", **log_kwargs)
        return False
    except AgentTokenError as exc:
        # Network / Keycloak failure on the client_credentials grant.
        # Transient by nature; the cron path retries on the next
        # scheduled instant, the one-off is consumed (at-most-once
        # contract). Logged at WARN so monitoring can alert on
        # sustained failures without parking the trigger.
        _log.warning(
            "scheduler_token_grant_failed",
            trigger_id=str(row.id),
            agent_name=prepared.name,
            reason=type(exc).__name__,
        )
        return False
    if outcome.status == AgentRunStatus.FAILED:
        # The trigger fired (the row was claimed/advanced and a terminal
        # run row exists) but the run was refused before the model call --
        # today the only such returned-FAILED outcome is the no-input
        # guard (#1505). Surface it at WARN with the typed error so the
        # misconfiguration is visible at fire time rather than masked by a
        # success-shaped ``scheduler_fired`` line. Still counts as a fire:
        # a one-off is consumed (at-most-once) and a cron has already
        # advanced -- the fix is operator-side (add ``inputs``), not a
        # scheduler retry.
        _log.warning(
            "scheduler_fired_run_failed",
            trigger_id=str(row.id),
            kind=row.kind,
            agent_name=prepared.name,
            agent_run_id=str(outcome.run_id),
            error=outcome.error,
        )
        return True
    _log.info(
        "scheduler_fired",
        trigger_id=str(row.id),
        kind=row.kind,
        agent_name=prepared.name,
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
