# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tenant-scoped :class:`RunbookRunService` over the G12.1 run storage substrate.

Initiative #1198 (G12.3) T3 surface. The REST routes (T5) and MCP tools
(T6) wrap this service rather than touching
:mod:`meho_backplane.db.models` directly, so the single-assignee
enforcement, the verify-at-advance gating, the audit-row plumbing, and
the post-completion ``show_template`` allowance predicate all live in
one place.

**This is the only module that writes to ``runbook_runs`` and
``runbook_run_step_states``.** Keeping every state-machine mutation in
one file makes the Initiative's adherence floor reviewable (one PR
covers every state transition).

Concurrency model
-----------------

:class:`RunbookRunService` is stateless and method-scoped: each public
method opens its own :class:`AsyncSession` via
:func:`~meho_backplane.db.engine.get_sessionmaker` and commits
synchronously before returning. Same posture as
:class:`meho_backplane.kb.service.KbService` and
:class:`meho_backplane.runbooks.service.RunbookTemplateService` — no
shared transaction state across calls.

Tenant scoping
--------------

Every public method takes ``tenant_id`` as the first parameter — no
contextvar resolution. The route / MCP layers (T5 / T6) bind the value
from the operator's JWT; the service is testable in isolation and the
tenant boundary is auditable at the call site.

RBAC
----

This service does **not** enforce *roles*. The senior-vs-operator gate
(operator can only ``start_run``; ``reassign_run`` is admin-only) is the
route / MCP boundary's job (T5 / T6). What this service **does**
enforce is the **single-assignee** invariant from Initiative #1198:
:meth:`next_step` refuses any caller other than ``run.assigned_to``
regardless of their role. The right way for a senior to take over a
junior's run is ``reassign_run``, not bypass; the service treats role
as orthogonal to ownership.

The :meth:`abort_run` allowance — "assignee OR any TENANT_ADMIN" — is
the only place the service reads the caller's role. The route layer
passes the ``caller_is_admin`` flag from the JWT; the service does not
do its own role lookup. Same shape used by :meth:`list_runs` so an
``OPERATOR`` sees only their runs while a ``TENANT_ADMIN`` sees all
tenant runs.

Verify-at-advance gating
------------------------

:meth:`next_step` refuses to advance if the current step's verify
predicate has not been satisfied. There is no skip / force-advance /
set-state path on this surface — the only way to bail from a stuck run
is :meth:`abort_run`; the only way to "fix" a stuck run is
:meth:`reassign_run` to a senior. The substrate is the verify oracle;
the caller's ``last_verified`` claim is informational only.

For ``verify.type='operation_call'``: the service binds
:data:`~meho_backplane.operations._audit.run_id_var` +
:data:`~meho_backplane.operations._audit.step_id_var` around
:func:`~meho_backplane.operations.meta_tools.call_operation`, so every
audit row written by the dispatched call carries the run / step
correlation columns populated. The :class:`OperationCallVerifyResponse`
the engine receives carries the call's raw result as ``actual``; the
engine constructs the persisted response with ``matched`` set to its
own structural-equality verdict (not the caller's claim).

Audit log
---------

* Every ``operation_call`` step dispatch lands one audit row via the
  dispatcher's standard write path — the run/step columns are
  populated because the contextvars are bound when the call fires.
* :meth:`abort_run` writes its own ``audit_log`` row directly (the
  abort is a meta-action, not an operation call). The row carries
  ``method='DISPATCH'``, ``path='runbook.abort'``, ``run_id`` set,
  ``step_id`` set (the step the run was on when aborted), and the
  abort reason in the JSON payload.

Substitution
------------

The engine (G12.3-T2, #1301) applies ``${run.target}`` /
``${run.params.X}`` to step bodies before returning them. The service
hands the run's ``target`` + ``params`` to the engine on every step
build; the engine itself owns the substitution pass. The service
re-substitutes the verify's ``op_id`` + ``params`` before dispatching
the verify call — the engine's step body has the substituted shape for
operator display, but the dispatch path needs the same substituted
values for the call payload.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AuditLog,
    EndpointDescriptor,
    RunbookRun,
    RunbookRunStepState,
    RunbookTemplate,
)
from meho_backplane.operations._audit import run_id_var, step_id_var, work_ref_var
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.runbooks.engine import (
    advance,
    current_step_body,
)
from meho_backplane.runbooks.runs_schemas import (
    AbortRunRequest,
    AbortRunResponse,
    CurrentStepResponse,
    ListRunsFilter,
    NextStepRequest,
    NextStepResponse,
    OperationCallVerifyResponse,
    ReassignRunRequest,
    ReassignRunResponse,
    RunCompletedResponse,
    RunSummary,
    StartRunRequest,
    StepPosition,
    VerifyResponse,
)
from meho_backplane.runbooks.schemas import (
    OperationCallStep,
    OperationCallVerify,
    RunbookTemplateBody,
    Step,
)
from meho_backplane.runbooks.service import (
    DeprecatedTemplateError,
    TemplateNotFoundError,
    _steps_from_storage,
)
from meho_backplane.runbooks.substitution import (
    RUN_PARAMS_PATTERN,
    resolve_substitutions,
)

__all__ = [
    "DeprecatedTemplateError",
    "MissingParamsError",
    "NotRunAssigneeError",
    "PreviousStepFailedError",
    "RunAlreadyTerminalError",
    "RunNotFoundError",
    "RunbookRunService",
]


#: The run state vocabulary. Mirrors the ``CheckConstraint`` on
#: :class:`~meho_backplane.db.models.RunbookRun`.
_RUN_STATES: frozenset[str] = frozenset({"in_progress", "completed", "abandoned"})
_TERMINAL_RUN_STATES: frozenset[str] = frozenset({"completed", "abandoned"})
_RunState = Literal["in_progress", "completed", "abandoned"]

#: The per-step state vocabulary. Mirrors the ``CheckConstraint`` on
#: :class:`~meho_backplane.db.models.RunbookRunStepState`.
_STEP_PENDING: str = "pending"
_STEP_IN_PROGRESS: str = "in_progress"
_STEP_VERIFIED: str = "verified"
_STEP_FAILED: str = "failed"

#: Audit-row constants for the direct :meth:`abort_run` write. The
#: ``method`` mirrors the dispatcher's ``'DISPATCH'`` value (chassis
#: convention; the run-level abort isn't an HTTP verb but using the
#: same string keeps audit queries uniform). ``path`` follows the
#: ``<surface>.<action>`` shape used by approval queue audit rows
#: (``approval.request`` / ``approval.decision``).
_ABORT_AUDIT_METHOD: str = "DISPATCH"
_ABORT_AUDIT_PATH: str = "runbook.abort"
_ABORT_AUDIT_STATUS_CODE: int = 200


class RunNotFoundError(LookupError):
    """``run_id`` doesn't resolve to a row in this tenant."""


class NotRunAssigneeError(PermissionError):
    """Caller is not the run's assignee. Use ``meho.runbook.reassign`` to take over.

    Raised by :meth:`RunbookRunService.next_step` for any caller other than
    ``run.assigned_to`` — including ``TENANT_ADMIN`` callers. The right way
    for a senior to take over a junior's run is :meth:`reassign_run`, not
    a role-based bypass (per Initiative #1198 single-assignee discipline).

    Also raised by :meth:`abort_run` when the caller is neither the
    assignee nor a tenant admin; the route layer passes
    ``caller_is_admin=True`` to widen the allowance.
    """


class RunAlreadyTerminalError(ValueError):
    """Run is already ``completed`` or ``abandoned`` — no further advance/abort allowed.

    Mutating a terminal run would invalidate the audit story (the abort or
    completion is the final transition; everything after is read-only). The
    service refuses the mutation rather than silently no-op'ing so the
    caller sees a clean error.
    """


class PreviousStepFailedError(ValueError):
    """Previous step is in ``failed`` state — operator must abort and start over.

    Raised by :meth:`RunbookRunService.next_step` when the current step's
    state is ``failed`` (the operator answered ``no`` / ``escalate`` on a
    confirm step, or the operation_call verify's actual did not match
    expect). The state machine forbids continuing past a failed step;
    :meth:`abort_run` is the only forward path.
    """


class MissingParamsError(ValueError):
    """``start_run`` was called without a params key referenced by the template.

    Defense-in-depth pre-check: the engine would raise ``KeyError`` at
    advance time when a ``${run.params.X}`` substitution can't resolve;
    catching the gap at start time gives the operator a typed error
    before the run row lands.
    """


@dataclass(frozen=True, slots=True)
class _NextStepInputs:
    """Pre-dispatch reads :meth:`RunbookRunService.next_step` carries across the gap.

    Captured under the read-only session A and consumed by the
    sessionless verify dispatch + the write-phase session B. The
    ``template_body`` / ``current_step`` are pinned at start_run and
    immutable for the run, so they remain valid after session A closes;
    ``run`` is re-loaded fresh inside session B (the captured instance
    is detached once its session closes and is only used for the
    contextvar-binding read in the dispatch phase).
    """

    run: RunbookRun
    template_body: RunbookTemplateBody
    current_step: Step
    current_step_id: str
    current_state: str
    run_target: str
    run_params: dict[str, Any]


class RunbookRunService:
    """Tenant-scoped lifecycle + audit for runbook runs.

    Stateless and async; instantiate once and call freely. Each public
    method opens its own DB session, commits, and closes — no shared
    transaction state across calls. The class ships with no constructor
    parameters; every dependency (the engine, the dispatcher, the
    contextvars) is bound via module-level singletons / imports.
    """

    async def start_run(
        self,
        tenant_id: uuid.UUID,
        operator_sub: str,
        request: StartRunRequest,
    ) -> CurrentStepResponse:
        """Begin a new run on the latest non-deprecated published template version.

        Resolves the template by *(tenant_id, request.template_slug)*,
        pinning to the latest ``published`` version. Refuses
        (``DeprecatedTemplateError``) if every version for the slug is
        ``deprecated``; refuses (``TemplateNotFoundError``) if no
        published or deprecated row exists.

        Pre-validates that every ``${run.params.X}`` referenced by the
        template's step bodies / op-call params / verify params + expect
        is present in ``request.params`` and raises
        :class:`MissingParamsError` otherwise — the engine would raise
        ``KeyError`` at the first ``next_step`` if we didn't catch it
        here, but failing at start time keeps the run row from landing.

        Inserts the run row with ``state='in_progress'`` and
        ``assigned_to=operator_sub``; inserts one
        :class:`~meho_backplane.db.models.RunbookRunStepState` row per
        step with the first marked ``in_progress`` and the rest
        ``pending``. Returns the first step's :class:`StepBody` (with
        ``${run.target}`` / ``${run.params.X}`` resolved) wrapped in a
        :class:`CurrentStepResponse`.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            template_row = await _load_published_or_raise(session, tenant_id, request.template_slug)
            template_body = _template_body_from_row(template_row)
            steps = template_body.steps

            self._validate_params_against_template(template_body, request.params)

            first_step = steps[0]
            now = datetime.now(UTC)
            run_id = uuid.uuid4()

            run = RunbookRun(
                run_id=run_id,
                tenant_id=tenant_id,
                template_slug=template_row.slug,
                template_version=template_row.version,
                assigned_to=operator_sub,
                target=request.target,
                params=dict(request.params),
                state="in_progress",
                work_ref=request.work_ref,
                started_by=operator_sub,
                started_at=now,
            )
            session.add(run)

            for index, step in enumerate(steps):
                state = _STEP_IN_PROGRESS if index == 0 else _STEP_PENDING
                started_at = now if index == 0 else None
                session.add(
                    RunbookRunStepState(
                        run_id=run_id,
                        step_id=step.id,
                        state=state,
                        started_at=started_at,
                    )
                )
            await session.commit()

        step_body = current_step_body(
            template_body,
            first_step.id,
            target=request.target,
            params=dict(request.params),
        )
        structlog.get_logger().info(
            "runbook_run_started",
            tenant_id=str(tenant_id),
            run_id=str(run_id),
            template_slug=template_row.slug,
            template_version=template_row.version,
            operator_sub=operator_sub,
        )
        return CurrentStepResponse(
            run_id=run_id,
            template_slug=template_row.slug,
            template_version=template_row.version,
            position=StepPosition(n=1, total=len(steps)),
            current_step=step_body,
        )

    async def next_step(
        self,
        tenant_id: uuid.UUID,
        operator_sub: str,
        run_id: uuid.UUID,
        request: NextStepRequest,
    ) -> NextStepResponse:
        """Advance the run one step, gated by the verify predicate.

        Refuses (:class:`NotRunAssigneeError`) when ``operator_sub`` is
        not ``run.assigned_to`` — including for ``TENANT_ADMIN`` callers
        (the right path is :meth:`reassign_run`). Refuses
        (:class:`RunAlreadyTerminalError`) when the run is already
        completed or abandoned. Refuses
        (:class:`PreviousStepFailedError`) when the current step's state
        is ``failed`` — the only forward path is :meth:`abort_run`.

        For ``verify.type='operation_call'``: binds
        :data:`run_id_var` + :data:`step_id_var` around
        :func:`call_operation`, dispatches the verify call with the
        substituted op_id + params, captures the result into an
        :class:`OperationCallVerifyResponse` whose ``actual`` is the
        call's raw ``result`` dict (the engine recomputes ``matched``).
        For ``verify.type='confirm'``: passes ``request.verify_response``
        through unchanged.

        Calls :func:`engine.advance` with the captured verify_response.
        Applies the outcome to the storage substrate:

        * ``kind='next_step'`` → mark current step ``verified`` with
          ``verified_at=now``, persist its ``verify_response``; mark the
          next step ``in_progress`` with ``started_at=now``. Returns a
          :class:`CurrentStepResponse` carrying the next step's body.
        * ``kind='completed'`` → mark current step ``verified``, persist
          its ``verify_response``; mark the run ``completed`` with
          ``completed_at=now``. Returns a :class:`RunCompletedResponse`.
        * ``kind='failed'`` → mark the current step ``failed``, persist
          its ``verify_response``. Raises :class:`PreviousStepFailedError`
          so the caller's next move is :meth:`abort_run` rather than a
          spurious retry on a step the state machine no longer accepts.

        Session lifetime: the method runs in three phases — a read-only
        session A (:meth:`_load_next_step_inputs`) for the pre-dispatch
        reads, a **sessionless** verify dispatch (so no pooled connection
        is pinned across the external ``operation_call``), and a write
        session B (:meth:`_write_next_step_outcome`) for the outcome. The
        run + step states are re-loaded and re-validated at the start of
        session B because a TENANT_ADMIN could have raced the dispatch
        with :meth:`abort_run` (→ :class:`RunAlreadyTerminalError`) or
        :meth:`reassign_run` (→ :class:`NotRunAssigneeError`); the
        single-assignee / no-mutate-terminal invariants must hold at the
        moment of the write, not just at the start of the call.
        """
        inputs = await self._load_next_step_inputs(tenant_id, operator_sub, run_id)

        # Dispatch the verify with NO session checked out.
        # ``template_body`` / ``current_step`` are pinned at start_run and
        # immutable for the run, so they survive the gap; the run/step
        # contextvar binding inside ``_resolve_verify_response`` is
        # task-local and independent of session lifetime.
        verify_response = await self._resolve_verify_response(
            run=inputs.run,
            current_step=inputs.current_step,
            request_verify=request.verify_response,
        )

        return await self._write_next_step_outcome(
            tenant_id, operator_sub, run_id, inputs, verify_response
        )

    async def _load_next_step_inputs(
        self,
        tenant_id: uuid.UUID,
        operator_sub: str,
        run_id: uuid.UUID,
    ) -> _NextStepInputs:
        """Phase A: read everything the dispatch + write need, under session A.

        Releases the pooled connection on block exit — SQLAlchemy 2.0's
        :class:`AsyncSession` holds its checked-out connection for the
        whole ``async with`` block, so awaiting the connector dispatch
        inside it would pin the connection for the full (multi-second)
        call and starve the pool under load.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session_a:
            run = await self._require_run_assignee(session_a, tenant_id, run_id, operator_sub)
            self._refuse_if_terminal(run)

            template_body = await self._load_pinned_template_body(session_a, run)
            step_states = await _load_step_states(session_a, run_id)
            current_step_id = self._current_step_id_or_raise(run, step_states)
            current_state = step_states[current_step_id].state

            if current_state == _STEP_FAILED:
                raise PreviousStepFailedError(
                    f"previous step {current_step_id!r} is in 'failed' state; abort the run",
                )

            return _NextStepInputs(
                run=run,
                template_body=template_body,
                current_step=_find_step(template_body, current_step_id),
                current_step_id=current_step_id,
                current_state=current_state,
                run_target=run.target,
                run_params=dict(run.params),
            )

    async def _write_next_step_outcome(
        self,
        tenant_id: uuid.UUID,
        operator_sub: str,
        run_id: uuid.UUID,
        inputs: _NextStepInputs,
        verify_response: VerifyResponse | None,
    ) -> NextStepResponse:
        """Phase C: advance + persist the outcome under a fresh session B.

        Re-loads and re-validates the run + step states: a TENANT_ADMIN
        could have raced the dispatch with :meth:`abort_run` (terminal
        flip) or :meth:`reassign_run` (assignee change). The
        single-assignee / no-mutate-terminal invariants must still hold
        at write time, not just at the start of the call.
        """
        now = datetime.now(UTC)
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session_b:
            run = await self._require_run_assignee(session_b, tenant_id, run_id, operator_sub)
            self._refuse_if_terminal(run)
            step_states = await _load_step_states(session_b, run_id)
            if step_states[inputs.current_step_id].state == _STEP_FAILED:
                raise PreviousStepFailedError(
                    f"previous step {inputs.current_step_id!r} is in 'failed' state; abort the run",
                )

            outcome = advance(
                inputs.template_body,
                inputs.current_step_id,
                inputs.current_state,
                target=inputs.run_target,
                params=inputs.run_params,
                verify_response=verify_response,
                completed_at=now,
            )
            return await self._apply_outcome(
                session=session_b,
                run=run,
                template_body=inputs.template_body,
                step_states=step_states,
                current_step_id=inputs.current_step_id,
                outcome=outcome,
                now=now,
                operator_sub=operator_sub,
            )

    async def _apply_outcome(
        self,
        *,
        session: AsyncSession,
        run: RunbookRun,
        template_body: RunbookTemplateBody,
        step_states: dict[str, RunbookRunStepState],
        current_step_id: str,
        outcome: Any,
        now: datetime,
        operator_sub: str,
    ) -> NextStepResponse:
        """Apply the engine's outcome to storage + return the response shape.

        Extracted from :meth:`next_step` to keep that method below the
        complexity threshold; the routing is pure I/O on the session +
        an attribute read on the outcome's ``kind``.
        """
        current_step_state = step_states[current_step_id]
        persisted_response_dict = _verify_response_to_storage(
            outcome.verify_response_persisted,
        )

        if outcome.kind == "failed":
            current_step_state.state = _STEP_FAILED
            current_step_state.verify_response = persisted_response_dict
            await session.commit()
            structlog.get_logger().info(
                "runbook_step_failed",
                tenant_id=str(run.tenant_id),
                run_id=str(run.run_id),
                step_id=current_step_id,
                operator_sub=operator_sub,
            )
            raise PreviousStepFailedError(
                f"step {current_step_id!r} verify did not pass; abort the run",
            )

        current_step_state.state = _STEP_VERIFIED
        current_step_state.verified_at = now
        current_step_state.verify_response = persisted_response_dict

        if outcome.kind == "completed":
            run.state = "completed"
            run.completed_at = outcome.completed_at or now
            completed_at_value = run.completed_at
            await session.commit()
            structlog.get_logger().info(
                "runbook_run_completed",
                tenant_id=str(run.tenant_id),
                run_id=str(run.run_id),
                template_slug=run.template_slug,
                template_version=run.template_version,
                operator_sub=operator_sub,
            )
            return RunCompletedResponse(
                run_id=run.run_id,
                completed_at=completed_at_value,
            )

        # outcome.kind == "next_step"
        next_step_body = outcome.next_step_body
        assert next_step_body is not None  # engine contract on next_step kind
        next_step_state = step_states[next_step_body.id]
        next_step_state.state = _STEP_IN_PROGRESS
        next_step_state.started_at = now
        await session.commit()

        structlog.get_logger().info(
            "runbook_step_advanced",
            tenant_id=str(run.tenant_id),
            run_id=str(run.run_id),
            from_step=current_step_id,
            to_step=next_step_body.id,
            operator_sub=operator_sub,
        )
        return CurrentStepResponse(
            run_id=run.run_id,
            template_slug=run.template_slug,
            template_version=run.template_version,
            position=_position_for_step(template_body, next_step_body.id),
            current_step=next_step_body,
        )

    async def abort_run(
        self,
        tenant_id: uuid.UUID,
        operator_sub: str,
        run_id: uuid.UUID,
        request: AbortRunRequest,
        *,
        caller_is_admin: bool = False,
    ) -> AbortRunResponse:
        """Mark the run ``abandoned`` and write an audit row with the reason.

        Permits caller ∈ ``{run.assigned_to, any TENANT_ADMIN}``. The
        ``caller_is_admin`` flag is passed by the route / MCP boundary
        based on the JWT's role claim; the service does not look up
        roles itself. Non-assignee non-admin callers get
        :class:`NotRunAssigneeError`.

        Refuses (:class:`RunAlreadyTerminalError`) when the run is
        already completed or abandoned — re-aborting a terminal run
        would invalidate the audit story (the original abort/completion
        is the canonical transition).

        Writes one ``audit_log`` row directly (not through the
        dispatcher) with ``method='DISPATCH'``, ``path='runbook.abort'``,
        ``run_id`` set, ``step_id`` set to the step the run was on when
        aborted, and the abort ``reason`` in the JSON payload. The
        direct write is what makes the abort a first-class audit event;
        Initiative #1198's "abort-with-audit" guarantee depends on this
        row landing.
        """
        sessionmaker = get_sessionmaker()
        abort_started = time.monotonic()
        async with sessionmaker() as session:
            run = await self._load_run_or_raise(session, tenant_id, run_id)
            if run.assigned_to != operator_sub and not caller_is_admin:
                raise NotRunAssigneeError(
                    f"caller {operator_sub!r} is not the assignee of run {run_id} "
                    f"and does not have TENANT_ADMIN; use meho.runbook.reassign to take over",
                )
            self._refuse_if_terminal(run)

            step_states = await _load_step_states(session, run_id)
            current_step_id = self._current_step_id_or_none(run, step_states)

            now = datetime.now(UTC)
            run.state = "abandoned"
            run.abandoned_at = now

            audit_row = self._build_abort_audit_row(
                tenant_id=tenant_id,
                operator_sub=operator_sub,
                run_id=run_id,
                step_id_at_abort=current_step_id,
                work_ref=run.work_ref,
                reason=request.reason,
                duration_ms=(time.monotonic() - abort_started) * 1000,
            )
            session.add(audit_row)
            await session.commit()

        structlog.get_logger().info(
            "runbook_run_aborted",
            tenant_id=str(tenant_id),
            run_id=str(run_id),
            operator_sub=operator_sub,
            step_id_at_abort=current_step_id,
        )
        return AbortRunResponse(
            run_id=run_id,
            abandoned_at=now,
        )

    async def reassign_run(
        self,
        tenant_id: uuid.UUID,
        operator_sub: str,
        run_id: uuid.UUID,
        request: ReassignRunRequest,
    ) -> ReassignRunResponse:
        """Transfer ownership of the run to a new assignee.

        Service-level: no caller role check. The TENANT_ADMIN gate is
        the route / MCP layer's job (T5 / T6). The service is willing to
        flip ``assigned_to`` for any caller; passing
        ``request.new_assignee == operator_sub`` is a no-op-shaped self-
        assignment (an admin takes over their own escalation handoff).

        Refuses (:class:`RunAlreadyTerminalError`) when the run is
        already completed or abandoned — a terminal run has no
        meaningful next-assignee.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            run = await self._load_run_or_raise(session, tenant_id, run_id)
            self._refuse_if_terminal(run)
            now = datetime.now(UTC)
            run.assigned_to = request.new_assignee
            await session.commit()

        structlog.get_logger().info(
            "runbook_run_reassigned",
            tenant_id=str(tenant_id),
            run_id=str(run_id),
            from_assignee=operator_sub,
            to_assignee=request.new_assignee,
        )
        return ReassignRunResponse(
            run_id=run_id,
            assigned_to=request.new_assignee,
            reassigned_at=now,
        )

    async def list_runs(
        self,
        tenant_id: uuid.UUID,
        caller_sub: str,
        *,
        caller_is_admin: bool,
        filter_: ListRunsFilter,
        limit: int = 100,
    ) -> list[RunSummary]:
        """List runs for *tenant_id*, scoped to the caller's visibility.

        ``caller_is_admin=False`` forces ``assignee=caller_sub`` regardless
        of what the filter says — an ``OPERATOR`` only ever sees their
        own runs even if they tried to filter to another assignee.
        ``caller_is_admin=True`` honours the filter as-is, so a
        ``TENANT_ADMIN`` can pass ``assignee=<other_sub>`` to inspect a
        junior's runs.

        Ordered by ``started_at`` descending and capped at *limit*. A
        ``limit=0`` returns an empty list without a query; a negative
        limit raises :class:`ValueError`.
        """
        if limit < 0:
            raise ValueError(f"limit must be >= 0; got {limit}")
        if limit == 0:
            return []

        # OPERATOR scope (``caller_is_admin=False``): always pin to
        # ``caller_sub`` regardless of what the filter says — the filter
        # is a *narrow* surface for the caller's own view, never an
        # *escape* into another operator's. ``TENANT_ADMIN`` callers
        # honour the filter as-is so they can audit any assignee.
        effective_assignee: str | None = filter_.assignee if caller_is_admin else caller_sub

        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            stmt = (
                select(RunbookRun)
                .where(RunbookRun.tenant_id == tenant_id)
                .order_by(RunbookRun.started_at.desc())
                .limit(limit)
            )
            if effective_assignee is not None:
                stmt = stmt.where(RunbookRun.assigned_to == effective_assignee)
            if filter_.status is not None:
                stmt = stmt.where(RunbookRun.state == filter_.status)
            if filter_.template_slug is not None:
                stmt = stmt.where(RunbookRun.template_slug == filter_.template_slug)
            if filter_.work_ref is not None:
                stmt = stmt.where(RunbookRun.work_ref == filter_.work_ref)
            rows = (await session.execute(stmt)).scalars().all()

            # Per-row current step + position need the pinned template body
            # for terminal-vs-in-progress disambiguation. Group rows by
            # (slug, version) so we load each template once.
            summaries: list[RunSummary] = []
            template_cache: dict[tuple[str, int], list[Step]] = {}
            for row in rows:
                key = (row.template_slug, row.template_version)
                if key not in template_cache:
                    pinned = await _load_pinned_template_or_none(session, tenant_id, *key)
                    template_cache[key] = pinned.steps if pinned is not None else []
                steps = template_cache[key]
                if row.state == "in_progress" and steps:
                    step_states = await _load_step_states(session, row.run_id)
                    current_step_id = self._current_step_id_or_none(row, step_states)
                    position = (
                        _position_from_step_id(steps, current_step_id)
                        if current_step_id is not None
                        else None
                    )
                else:
                    current_step_id = None
                    position = None
                summaries.append(
                    RunSummary(
                        run_id=row.run_id,
                        template_slug=row.template_slug,
                        template_version=row.template_version,
                        assigned_to=row.assigned_to,
                        target=row.target,
                        state=_narrow_run_state(row.state),
                        started_at=row.started_at,
                        completed_at=row.completed_at,
                        abandoned_at=row.abandoned_at,
                        current_step_id=current_step_id,
                        position=position,
                        work_ref=row.work_ref,
                    )
                )
            return summaries

    async def get_current_step(
        self,
        tenant_id: uuid.UUID,
        operator_sub: str,
        run_id: uuid.UUID,
    ) -> CurrentStepResponse | RunCompletedResponse | AbortRunResponse:
        """Read the run's current step (opacity-safe), or its terminal state.

        The read-side analogue of :meth:`start_run` / :meth:`next_step`:
        the BFF run **driver** page (``GET /ui/runbooks/runs/{run_id}``,
        #1893) needs a renderable current step on a fresh navigation /
        browser refresh, but neither :meth:`list_runs` (no body) nor the
        POST advance handlers (only fire on an action) yield one. This
        getter closes that gap.

        It returns exactly the same single-step projection the advance
        path returns — a :class:`CurrentStepResponse` carrying one
        :class:`StepBody` (``${run.target}`` / ``${run.params.X}``
        resolved by the engine) plus the :class:`StepPosition` hint — for
        an ``in_progress`` run. For a terminal run it returns the matching
        terminal-state shape (:class:`RunCompletedResponse` for
        ``completed``, :class:`AbortRunResponse` for ``abandoned``) so the
        driver can render the completed / abandoned banner without a body.

        **This method is the opacity guard for the read path.** It builds
        the response through :func:`engine.current_step_body` — the
        single-step opacity function — and never returns
        ``template_body.steps`` or any structural hint about adjacent
        positions. The pinned body is loaded internally only to resolve
        the one current step + its position (exactly as :meth:`start_run`
        does); it is not surfaced. Re-deriving the current step from the
        full pinned body in the handler / template would re-open the
        skip-ahead leak Initiative #1198 (G12) closed — this method exists
        so the handler never has to touch the step list.

        No role / assignee gate: reading the current step is the same
        operator-floor read as :meth:`list_runs` (the BFF session gate is
        the floor). The assignee gate is a **write**-side invariant on
        :meth:`next_step`; the driver shows the step to any operator who
        can see the run and gates the *Advance* control separately.

        Raises :class:`RunNotFoundError` when *run_id* does not resolve in
        this tenant (the driver maps it to a 404 page).
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            run = await self._load_run_or_raise(session, tenant_id, run_id)

            if run.state == "completed":
                # Terminal: no current step. The completed-at timestamp is
                # the canonical column set when ``next_step`` flipped the run.
                return RunCompletedResponse(
                    run_id=run.run_id,
                    completed_at=run.completed_at or run.started_at,
                )
            if run.state == "abandoned":
                return AbortRunResponse(
                    run_id=run.run_id,
                    abandoned_at=run.abandoned_at or run.started_at,
                )

            template_body = await self._load_pinned_template_body(session, run)
            step_states = await _load_step_states(session, run_id)
            current_step_id = self._current_step_id_or_raise(run, step_states)

        # Build the single opaque StepBody outside the session (pure CPU on
        # the already-loaded body). ``current_step_body`` returns exactly one
        # step — there is no overload that leaks the surrounding list.
        step_body = current_step_body(
            template_body,
            current_step_id,
            target=run.target,
            params=dict(run.params),
        )
        return CurrentStepResponse(
            run_id=run.run_id,
            template_slug=run.template_slug,
            template_version=run.template_version,
            position=_position_for_step(template_body, current_step_id),
            current_step=step_body,
        )

    async def get_run_assignee(
        self,
        tenant_id: uuid.UUID,
        run_id: uuid.UUID,
    ) -> str:
        """Return the run's current ``assigned_to`` subject (opacity-neutral).

        The BFF driver page (#1893) needs the assignee to decide whether to
        render the *Advance* control — it is shown only when
        ``session.operator_sub == assigned_to``. The assignee is already
        surfaced on :class:`RunSummary` via :meth:`list_runs`, but that path
        is role-scoped (an operator's ``list_runs`` omits runs assigned to
        someone else) and returns a list; this focused read returns just the
        one field for the one run, with no opacity surface widened (it
        exposes no step content).

        The control this gates is a UX hint only: the real enforcement is
        :meth:`next_step` raising :class:`NotRunAssigneeError` fail-closed
        for any non-assignee (including a TENANT_ADMIN). Hiding the button
        is convenience; the service is the authority.

        Raises :class:`RunNotFoundError` when *run_id* does not resolve in
        this tenant.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            run = await self._load_run_or_raise(session, tenant_id, run_id)
            return run.assigned_to

    async def can_show_template_post_completion(
        self,
        tenant_id: uuid.UUID,
        operator_sub: str,
        template_slug: str,
        template_version: int,
    ) -> bool:
        """Return ``True`` iff *operator_sub* has finished a run of *(slug, version)*.

        "Finished" is ``state ∈ {completed, abandoned}`` — an
        ``in_progress`` run does **not** unlock the read (the opacity
        floor stays in place while the run is live). The predicate is
        the lookup G12.3-T4 (#1309) wires into the
        ``meho.runbook.show_template`` 403 path: an ``OPERATOR`` who has
        completed or abandoned a run against this exact pinned
        ``(slug, version)`` can read the template for post-mortem; an
        ``OPERATOR`` with no such run still gets 403.

        Pure predicate, no row mutation. The service does not enforce
        whose role the caller has — the route / MCP layer applies this
        gate only after the role gate has already produced its 403.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            stmt = (
                select(func.count())
                .select_from(RunbookRun)
                .where(
                    RunbookRun.tenant_id == tenant_id,
                    RunbookRun.assigned_to == operator_sub,
                    RunbookRun.template_slug == template_slug,
                    RunbookRun.template_version == template_version,
                    RunbookRun.state.in_(("completed", "abandoned")),
                )
            )
            count = await session.scalar(stmt)
        return bool(count)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _require_run_assignee(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        run_id: uuid.UUID,
        caller_sub: str,
    ) -> RunbookRun:
        """Load the run, raise :class:`NotRunAssigneeError` if caller != assignee.

        ``TENANT_ADMIN`` callers go through :meth:`reassign_run`, not
        bypass — discipline matches the Initiative's single-assignee
        invariant. The :class:`RunNotFoundError` for an unknown run is
        layered under the role check so a non-assignee probing run ids
        sees the same shape (``NotRunAssigneeError``) regardless of
        whether the id exists.
        """
        run = await self._load_run_or_raise(session, tenant_id, run_id)
        if run.assigned_to != caller_sub:
            raise NotRunAssigneeError(
                f"caller {caller_sub!r} is not the assignee of run {run_id}; "
                f"use meho.runbook.reassign to take over",
            )
        return run

    async def _load_run_or_raise(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        run_id: uuid.UUID,
    ) -> RunbookRun:
        """Return the run or raise :class:`RunNotFoundError`."""
        stmt = select(RunbookRun).where(
            RunbookRun.tenant_id == tenant_id,
            RunbookRun.run_id == run_id,
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise RunNotFoundError(f"no run {run_id} for tenant")
        return row

    def _refuse_if_terminal(self, run: RunbookRun) -> None:
        """Raise :class:`RunAlreadyTerminalError` if the run is non-mutable."""
        if run.state in _TERMINAL_RUN_STATES:
            raise RunAlreadyTerminalError(
                f"run {run.run_id} is already in state {run.state!r}; no mutation allowed",
            )

    async def _load_pinned_template_body(
        self,
        session: AsyncSession,
        run: RunbookRun,
    ) -> RunbookTemplateBody:
        """Load the pinned ``(slug, version)`` template body.

        Runs are pinned at start time; later template edits cannot alter
        an in-flight run's step list (Initiative #1198 deprecation
        interplay). Reads the exact pinned row.
        """
        pinned = await _load_pinned_template_or_none(
            session, run.tenant_id, run.template_slug, run.template_version
        )
        if pinned is None:
            raise TemplateNotFoundError(
                f"pinned template {run.template_slug!r} v{run.template_version} "
                f"not found for run {run.run_id}",
            )
        return pinned

    def _current_step_id_or_raise(
        self,
        run: RunbookRun,
        step_states: dict[str, RunbookRunStepState],
    ) -> str:
        """Return the id of the step currently ``in_progress`` or ``failed``.

        Picks the step that the next state-machine transition targets:
        the lone ``in_progress`` step in normal flow, or a ``failed`` step
        (so the failure can be surfaced cleanly). Raises ``ValueError``
        when no candidate exists — which shouldn't happen for an
        ``in_progress`` run because :meth:`start_run` always inserts one
        ``in_progress`` row.
        """
        candidate = self._current_step_id_or_none(run, step_states)
        if candidate is None:
            raise ValueError(
                f"run {run.run_id} has no in_progress or failed step (state={run.state!r})",
            )
        return candidate

    def _current_step_id_or_none(
        self,
        run: RunbookRun,
        step_states: dict[str, RunbookRunStepState],
    ) -> str | None:
        """Same lookup as :meth:`_current_step_id_or_raise` but returns ``None``.

        Used by :meth:`abort_run` (a fully-pending run with zero step rows
        in flight legitimately has no current step) and by :meth:`list_runs`
        (terminal runs have no current step).
        """
        for state in (_STEP_IN_PROGRESS, _STEP_FAILED):
            for step_id, step_state in step_states.items():
                if step_state.state == state:
                    return step_id
        return None

    async def _resolve_verify_response(
        self,
        *,
        run: RunbookRun,
        current_step: Step,
        request_verify: VerifyResponse | None,
    ) -> VerifyResponse | None:
        """Produce the verify response the engine should evaluate.

        For ``verify.type='confirm'``: pass the caller's response
        through unchanged (the engine's
        :func:`~meho_backplane.runbooks.engine._evaluate_confirm` handles
        missing-response / shape-mismatch as typed engine errors).

        For ``verify.type='operation_call'``: dispatch the verify call
        with run/step contextvars bound, capture the result, return an
        :class:`OperationCallVerifyResponse` whose ``actual`` carries
        the call's result dict. ``matched`` is set to ``False``
        provisionally — the engine recomputes it against the substituted
        ``expect``.
        """
        verify = current_step.verify
        if not isinstance(verify, OperationCallVerify):
            return request_verify

        params = dict(run.params)
        substituted_op_id = _substitute_string(verify.op_id, target=run.target, params=params)
        substituted_params_obj = resolve_substitutions(
            verify.params, target=run.target, params=params
        )
        assert isinstance(substituted_params_obj, dict)

        connector_id = await self._resolve_connector_id_for_op(
            tenant_id=run.tenant_id, op_id=substituted_op_id
        )

        operator = _build_operator_for_dispatch(
            sub=run.assigned_to,
            tenant_id=run.tenant_id,
        )
        call_arguments: dict[str, Any] = {
            "connector_id": connector_id,
            "op_id": substituted_op_id,
            "target": run.target,
            "params": dict(substituted_params_obj),
        }

        # Bind run / step / work_ref onto the shared ContextVars around the
        # dispatch so the dispatcher's audit writer stamps them on the
        # operation_call step's audit row. ``work_ref`` is the run's
        # durable change-ticket reference (work_ref I3-T1 #1661): binding
        # it here -- once per step, from the run row -- is the "bind once,
        # inherit" boundary that gives every dispatching step's audit row
        # the same ``work_ref`` without each step having to carry it.
        run_token = run_id_var.set(run.run_id)
        step_token = step_id_var.set(current_step.id)
        work_ref_token = work_ref_var.set(run.work_ref)
        try:
            call_result = await call_operation(operator, call_arguments)
        finally:
            work_ref_var.reset(work_ref_token)
            step_id_var.reset(step_token)
            run_id_var.reset(run_token)

        actual = call_result.get("result", {})
        if not isinstance(actual, dict):
            # The verify's _matches() compares dict-shaped expect against
            # dict-shaped actual; a list/scalar result from a typed handler
            # surfaces as a non-match through the engine. Wrap the value
            # so the persisted row still carries the raw payload for
            # forensics.
            actual = {"result": actual}
        return OperationCallVerifyResponse(
            type="operation_call",
            matched=False,
            actual=actual,
        )

    async def _resolve_connector_id_for_op(
        self,
        *,
        tenant_id: uuid.UUID,
        op_id: str,
    ) -> str:
        """Resolve ``op_id`` to a ``connector_id`` via the descriptor table.

        The runbook step's verify schema carries only ``op_id`` — the
        connector identity is encoded in the matching
        :class:`~meho_backplane.db.models.EndpointDescriptor` row's
        ``(product, version, impl_id)`` triple. We reconstruct the
        ``connector_id`` form the dispatcher expects
        (``<impl_id>-<version>`` per
        :func:`~meho_backplane.operations._lookup.parse_connector_id`)
        so the call goes through the standard dispatch path.

        Tenant scoping: tenant-scoped descriptors win when present;
        falls back to the global ``tenant_id IS NULL`` row. Same shape
        as :func:`~meho_backplane.operations._lookup.lookup_descriptor`
        but without a connector-id pre-filter (the runbook's authoring
        contract is op-id-only at the verify level).

        Raises :class:`TemplateNotFoundError` when no descriptor
        matches — the run failed to dispatch its verify call because
        the operation no longer exists. The error type is borrowed from
        the template service vocabulary for consistency; a future
        refactor can introduce a more specific error if the route /
        MCP boundary needs to disambiguate.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            stmt = (
                select(EndpointDescriptor)
                .where(
                    EndpointDescriptor.op_id == op_id,
                    EndpointDescriptor.is_enabled.is_(True),
                    or_(
                        EndpointDescriptor.tenant_id == tenant_id,
                        EndpointDescriptor.tenant_id.is_(None),
                    ),
                )
                # Tenant-scoped wins over global when both exist; the
                # nulls-last ordering lets the first row pinpoint the
                # right one without a second query.
                .order_by(EndpointDescriptor.tenant_id.is_(None).asc())
                .limit(1)
            )
            descriptor = (await session.execute(stmt)).scalar_one_or_none()
        if descriptor is None:
            raise TemplateNotFoundError(
                f"no enabled endpoint_descriptor for op_id {op_id!r} in tenant scope; "
                f"cannot dispatch verify call",
            )
        return _format_connector_id(impl_id=descriptor.impl_id, version=descriptor.version)

    def _validate_params_against_template(
        self, body: RunbookTemplateBody, params: dict[str, object]
    ) -> None:
        """Raise :class:`MissingParamsError` if a referenced ``${run.params.X}`` is absent.

        Defense in depth — the engine raises ``KeyError`` at the first
        ``next_step`` if it can't resolve a substitution. Catching at
        start time gives the operator a typed error before the run row
        lands; the engine path stays the canonical oracle (the engine
        re-validates on every step).
        """
        referenced: set[str] = set()
        for step in body.steps:
            _collect_param_refs(step.body, referenced)
            verify = step.verify
            if isinstance(verify, OperationCallVerify):
                _collect_param_refs(verify.op_id, referenced)
                _walk_collect(verify.params, referenced)
                _walk_collect(verify.expect, referenced)
            else:
                _collect_param_refs(verify.prompt, referenced)
            if isinstance(step, OperationCallStep):
                _collect_param_refs(step.op_id, referenced)
                _walk_collect(step.params, referenced)
        missing = referenced - set(params.keys())
        if missing:
            sorted_missing = sorted(missing)
            raise MissingParamsError(
                f"template references run.params not supplied at start: "
                f"{', '.join(sorted_missing)}",
            )

    def _build_abort_audit_row(
        self,
        *,
        tenant_id: uuid.UUID,
        operator_sub: str,
        run_id: uuid.UUID,
        step_id_at_abort: str | None,
        work_ref: str | None,
        reason: str,
        duration_ms: float,
    ) -> AuditLog:
        """Compose the direct audit row for :meth:`abort_run`.

        Uses ``method='DISPATCH'`` to keep audit queries uniform (the
        dispatcher uses the same value; chassis HTTP audit rows use
        ``GET`` / ``POST`` etc.; the dispatcher convention is what
        runbook meta-actions share). ``path='runbook.abort'`` follows
        the ``<surface>.<action>`` shape used by approval queue rows.

        The run-correlation columns (``run_id`` / ``step_id`` /
        ``work_ref``) are populated on the row directly — not via the
        contextvar binding the dispatcher uses — because the abort is
        not an operation dispatch; the columns are documented to carry
        the run/step the row pertains to regardless of how the row
        landed (#1294). ``work_ref`` is the run's change-ticket
        reference, so the abort event is correlated to the same ticket
        as the run's dispatching steps (work_ref I3-T1 #1661).
        """
        payload = {
            "op_id": _ABORT_AUDIT_PATH,
            "result_status": "ok",
            "reason": reason,
            "run_id": str(run_id),
        }
        if step_id_at_abort is not None:
            payload["step_id"] = step_id_at_abort

        kwargs: dict[str, Any] = {
            "id": uuid.uuid4(),
            "occurred_at": datetime.now(UTC),
            "operator_sub": operator_sub,
            "tenant_id": tenant_id,
            "method": _ABORT_AUDIT_METHOD,
            "path": _ABORT_AUDIT_PATH,
            "status_code": _ABORT_AUDIT_STATUS_CODE,
            "duration_ms": Decimal(str(round(duration_ms, 2))),
            "payload": payload,
            "raw_payload": {"reason": reason},
        }
        # Same forward-compat guard the dispatcher uses for the
        # ``run_id`` / ``step_id`` columns (#1294): only set the kwargs
        # when the model carries the column, so this module is buildable
        # against schemas that predate migration 0034.
        if hasattr(AuditLog, "run_id"):
            kwargs["run_id"] = run_id
        if hasattr(AuditLog, "step_id"):
            kwargs["step_id"] = step_id_at_abort
        # work_ref I3-T1 #1661: same forward-compat guard -- the column is
        # added by migration 0039 and is None unless the run was started
        # with a change ticket.
        if hasattr(AuditLog, "work_ref"):
            kwargs["work_ref"] = work_ref
        return AuditLog(**kwargs)


# ---------------------------------------------------------------------------
# Module-level helpers (not part of the public surface).
# ---------------------------------------------------------------------------


async def _load_published_or_raise(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    slug: str,
) -> RunbookTemplate:
    """Return the latest ``published`` row for *slug*, classifying errors.

    Walks the slug's version history:

    * No rows for the slug → :class:`TemplateNotFoundError`.
    * Some rows exist but none are ``published`` and none are
      ``deprecated`` (the slug is draft-only) →
      :class:`TemplateNotFoundError`.
    * Some rows exist; every one is ``deprecated`` (no published
      version) → :class:`DeprecatedTemplateError`.
    * At least one ``published`` row exists → return the highest-
      version such row.

    The branching matches the spec from #1308: start refuses
    deprecated, refuses unpublished, but advances against the latest
    non-deprecated published version.
    """
    stmt = (
        select(RunbookTemplate)
        .where(
            RunbookTemplate.tenant_id == tenant_id,
            RunbookTemplate.slug == slug,
        )
        .order_by(RunbookTemplate.version.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    if not rows:
        raise TemplateNotFoundError(f"no template for slug {slug!r}")

    published = [row for row in rows if row.status == "published"]
    if published:
        return published[0]

    deprecated = [row for row in rows if row.status == "deprecated"]
    if deprecated and not any(row.status == "draft" for row in rows):
        raise DeprecatedTemplateError(
            f"every version of template {slug!r} is deprecated; cannot start a new run",
        )
    raise TemplateNotFoundError(
        f"no published version of template {slug!r} (only drafts exist)",
    )


async def _load_pinned_template_or_none(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    slug: str,
    version: int,
) -> RunbookTemplateBody | None:
    """Return the pinned template body or ``None`` when the row is missing.

    The runbook engine wants the validated :class:`RunbookTemplateBody`,
    not the raw row — running the steps through
    :func:`~meho_backplane.runbooks.service._steps_from_storage`
    round-trips them through Pydantic so substitution + step-id
    uniqueness are re-asserted at read time.
    """
    stmt = select(RunbookTemplate).where(
        RunbookTemplate.tenant_id == tenant_id,
        RunbookTemplate.slug == slug,
        RunbookTemplate.version == version,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return None
    return _template_body_from_row(row)


def _template_body_from_row(row: RunbookTemplate) -> RunbookTemplateBody:
    """Round-trip a stored row into a validated :class:`RunbookTemplateBody`."""
    return RunbookTemplateBody(
        title=row.title,
        description=row.description,
        target_kind=row.target_kind,
        steps=_steps_from_storage(row.steps),
    )


async def _load_step_states(
    session: AsyncSession,
    run_id: uuid.UUID,
) -> dict[str, RunbookRunStepState]:
    """Return a ``{step_id: state}`` map for every step row of *run_id*.

    Maintains identity-mapped rows so callers can mutate ``state`` /
    ``started_at`` / ``verified_at`` / ``verify_response`` in place and
    the session's pending-flush picks them up on the next ``commit()``.
    """
    stmt = select(RunbookRunStepState).where(RunbookRunStepState.run_id == run_id)
    rows = (await session.execute(stmt)).scalars().all()
    return {row.step_id: row for row in rows}


def _find_step(template_body: RunbookTemplateBody, step_id: str) -> Step:
    """Return the step with *step_id* or raise :class:`KeyError`.

    Duplicates :func:`~meho_backplane.runbooks.engine._find_step` because
    that helper is engine-private; copying the four-line lookup keeps
    the engine module decoupled from service-layer details.
    """
    for step in template_body.steps:
        if step.id == step_id:
            return step
    raise KeyError(step_id)


def _position_for_step(template_body: RunbookTemplateBody, step_id: str) -> StepPosition:
    """1-indexed :class:`StepPosition` for *step_id* within *template_body.steps*.

    Raises :class:`KeyError` when *step_id* is absent — only callable for
    a *step_id* the engine just produced, so absence indicates a
    contract violation upstream rather than a data condition.
    """
    position = _position_from_step_id(template_body.steps, step_id)
    if position is None:
        raise KeyError(step_id)
    return position


def _position_from_step_id(steps: list[Step], step_id: str | None) -> StepPosition | None:
    """1-indexed :class:`StepPosition` for *step_id*; ``None`` when absent.

    ``None`` for *step_id* short-circuits to ``None`` so the caller can
    chain the call without an explicit guard.
    """
    if step_id is None:
        return None
    for index, step in enumerate(steps):
        if step.id == step_id:
            return StepPosition(n=index + 1, total=len(steps))
    return None


def _verify_response_to_storage(
    response: VerifyResponse | None,
) -> dict[str, Any] | None:
    """Serialise a :class:`VerifyResponse` to the JSONB column shape.

    Returns ``None`` when *response* is ``None`` (no verify required, or
    the engine produced no persisted response). The ``model_dump`` mode
    matches the convention :class:`~meho_backplane.db.models.RunbookRunStepState`
    accepts for its ``_PORTABLE_JSON`` column.
    """
    if response is None:
        return None
    return response.model_dump(mode="json")


def _format_connector_id(*, impl_id: str, version: str) -> str:
    """Inverse of :func:`~meho_backplane.operations._lookup.parse_connector_id`.

    Builds the ``<impl_id>-<version>`` form when both parts are populated;
    falls back to ``impl_id`` alone for the v1-style single-product
    registration (where ``impl_id`` equals the product and ``version``
    is empty). Either form parses cleanly through ``parse_connector_id``
    so the dispatcher's lookup remains symmetric.
    """
    if not version:
        return impl_id
    return f"{impl_id}-{version}"


def _build_operator_for_dispatch(
    *,
    sub: str,
    tenant_id: uuid.UUID,
) -> Any:
    """Construct an :class:`~meho_backplane.auth.operator.Operator` for the call.

    The dispatch path requires a populated ``Operator`` (the audit row
    uses ``operator.sub`` + ``operator.tenant_id``). The runbook
    service's public API takes ``operator_sub: str`` — by convention
    the route / MCP layer has the JWT-validated operator and we
    reconstruct the minimum-shape object here.

    ``raw_jwt`` is the **empty string** — the fail-closed synthetic-
    operator convention (the topology scheduler's refresh operator uses
    the same shape). This operator is reconstructed from
    ``operator_sub``, not validated from a bearer token, so it must
    never be usable as operator context for a downstream credential
    read: every Vault-touching layer short-circuits on an empty
    ``raw_jwt`` (``_resolve_secret_ref`` in
    :mod:`meho_backplane.connectors._shared.vault_creds` raises
    ``VaultCredentialsReadError`` before Vault is contacted). A verify
    ``op_id`` that resolves to a Vault-backed connector therefore fails
    closed locally with a structured refusal instead of forwarding a
    placeholder string to Vault's JWT/OIDC login. Typed in-process
    verify handlers that need no per-target credential read never
    consume ``raw_jwt`` and dispatch unchanged.

    Imports lazily to avoid a hard import-cycle at module load (the
    auth subpackage depends on a lot of substrate; runbooks initialises
    earlier in the dependency graph).
    """
    from meho_backplane.auth.operator import Operator, TenantRole

    return Operator(
        sub=sub,
        name=sub,
        email=None,
        raw_jwt="",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


def _substitute_string(value: str, *, target: str, params: dict[str, object]) -> str:
    """Type-narrow wrapper around :func:`resolve_substitutions` for ``str`` input."""
    resolved = resolve_substitutions(value, target=target, params=params)
    assert isinstance(resolved, str)
    return resolved


def _collect_param_refs(value: str, sink: set[str]) -> None:
    """Append every ``${run.params.X}`` name found in *value* to *sink*.

    Reads from T2's compiled :data:`RUN_PARAMS_PATTERN` (the regex
    canon); collects matching param names so the start-time validator
    can compare against the request's ``params`` dict in one pass.
    ``${run.target}`` substitutions are unconditionally satisfied (the
    request body's ``target`` field is required) and so are not tracked
    here.
    """
    for match in RUN_PARAMS_PATTERN.finditer(value):
        sink.add(match.group(1))


def _walk_collect(value: object, sink: set[str]) -> None:
    """Recursive :func:`_collect_param_refs` over ``str`` / ``dict`` / ``list`` nodes."""
    if isinstance(value, str):
        _collect_param_refs(value, sink)
    elif isinstance(value, dict):
        for sub in value.values():
            _walk_collect(sub, sink)
    elif isinstance(value, list):
        for item in value:
            _walk_collect(item, sink)


def _narrow_run_state(state: str) -> _RunState:
    """Narrow the storage-layer ``str`` to the :class:`RunSummary`'s ``Literal``.

    Same defensive narrowing :func:`meho_backplane.runbooks.service._narrow_status`
    applies on the template side: the DB ``CheckConstraint`` guarantees
    the closed vocabulary at write time, but a hand-edited row or a
    future migration gap would surface as a clean ``ValueError`` here
    rather than as a Pydantic validation error one layer up.
    """
    if state not in _RUN_STATES:
        raise ValueError(f"unexpected run state: {state!r}")
    return state  # type: ignore[return-value]
