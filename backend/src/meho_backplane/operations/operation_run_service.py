# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Async governed dispatch runner — submit / poll / cancel (#3079).

The execution engine behind async governed dispatch. A ``POST
/api/v1/operations/call`` submitted with ``async=true`` (or an approval
resumed in async mode) hands off here: the service creates a durable
:class:`~meho_backplane.db.models.OperationRun` row, launches the governed
dispatch as a background :class:`asyncio.Task`, and returns the run id (the
handle) immediately so the HTTP layer can answer 202. The caller then polls
:meth:`poll` / cancels :meth:`cancel` via the handle; a completed run's full
:class:`~meho_backplane.connectors.schemas.OperationResult` envelope is
persisted on the row and returned by the poll -- so a dropped response never
loses the outcome (the motivating incident: an 83s vendor call whose 200 was
lost in transit).

This mirrors the shape of
:class:`meho_backplane.agent.invocation.AgentInvoker`'s background-run
machinery (durable row + in-process task store + lease heartbeat sidecar +
reaper reclaim) applied to a single governed dispatch instead of an LLM
tool-use loop. The governed dispatch runs through the *same*
:func:`~meho_backplane.operations.meta_tools.call_operation` /
:func:`~meho_backplane.operations.approval_queue.resume_dispatch_after_approval`
path a synchronous request uses -- identical policy gate, identical
synchronous audit write -- so audit stays per-operation, append-only
(v0.1-spec §6): the run is marked ``succeeded`` only *after* the dispatch
(and its committed audit row) returns.

Why the dispatch envelope, not an exception, is the success signal
----------------------------------------------------------------------

``call_operation`` / the resume path are contracted to *always return a
structured envelope* (``status`` of ``ok`` / ``error`` / ``denied`` /
``needs-approval``) and never raise for operator-input faults. So a run
that completes is ``succeeded`` and its envelope -- whatever its own status
-- is persisted. ``failed`` is reserved for a run whose worker died (reaped)
or whose dispatch raised unexpectedly (defence-in-depth); those never
produced an envelope.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    OperationRun,
    OperationRunOrigin,
    OperationRunStatus,
)
from meho_backplane.operations import operation_run as run_lifecycle
from meho_backplane.settings import get_settings

__all__ = [
    "OperationRunService",
    "get_operation_run_service",
]

_log = structlog.get_logger(__name__)

#: A zero-arg coroutine that runs the governed dispatch and returns its
#: envelope (either an ``OperationResult`` or its ``model_dump`` dict).
_DispatchCoro = Callable[[], Awaitable[Any]]


@dataclass(slots=True)
class _RunState:
    """In-process liveness anchor for one background run.

    Holds a strong reference to the task so it is not garbage-collected
    mid-flight (asyncio keeps only a weak reference to a bare task). The
    store evicts the entry on completion via the task's done-callback.
    """

    task: asyncio.Task[None]


def _lease_owner() -> str:
    """Compute a stable per-process ``lease_owner`` (``"<hostname>:<pid>"``).

    Same shape the agent-run invoker uses; an operator chasing a reaped run
    maps the reaper-recorded ``prior_lease_owner`` back to the pod/process
    whose worker died. No PII, no secret material.
    """
    return f"{socket.gethostname()}:{os.getpid()}"


def _target_name(target: Any) -> str | None:
    """Extract the submitted target *name* from a ``/call`` target arg.

    The ``/call`` body accepts a bare string, a ``{"name": ...}`` dict, or
    ``None`` (target-less op). We persist only the name for display on the
    poll surface -- not the resolved id (resolution happens per-dispatch).
    """
    if isinstance(target, str):
        return target or None
    if isinstance(target, dict):
        name = target.get("name")
        return name if isinstance(name, str) and name else None
    return None


def _normalize_envelope(value: Any) -> dict[str, Any]:
    """Coerce a dispatch return (``OperationResult`` | dict) to a JSON dict."""
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")  # type: ignore[no-any-return]
    return {"status": "error", "error": "dispatch returned a non-serializable result"}


class OperationRunService:
    """Submit / poll / cancel async governed operation dispatches.

    A process-wide singleton (see :func:`get_operation_run_service`) holding
    the in-process task store. The store keeps a strong reference to each
    background task so it survives the request that created it; a
    done-callback evicts finished runs so a long-lived worker does not
    accumulate them. The durable ``operation_run`` row is the source of
    truth -- :meth:`poll` reads it, so it works after the in-memory task is
    gone (a different pod, a restarted process).
    """

    def __init__(self) -> None:
        self._store: dict[uuid.UUID, _RunState] = {}

    # -- submit -----------------------------------------------------------

    async def submit_call(self, operator: Operator, arguments: dict[str, Any]) -> uuid.UUID:
        """Create a durable run for an async ``/operations/call`` and launch it.

        Persists a ``pending`` row (origin ``direct``) with a lease claimed
        by this worker, then launches the governed dispatch
        (:func:`~meho_backplane.operations.meta_tools.call_operation`) as a
        background task. Returns the run id (the 202 handle) immediately.

        ``arguments`` is the ``call_operation`` shape: ``{"connector_id",
        "op_id", "target"?, "params"?, "work_ref"?}``. Only a ``params_hash``
        is persisted, never the raw params (the run never resumes, and a
        params blob would be a new secret surface).
        """
        from meho_backplane.operations._validate import compute_params_hash
        from meho_backplane.operations.meta_tools import call_operation

        connector_id = str(arguments["connector_id"])
        op_id = str(arguments["op_id"])
        params: dict[str, Any] = arguments.get("params") or {}
        params_hash = compute_params_hash(params) if params else None

        owner = _lease_owner()
        run_id = await self._create_and_claim(
            operator,
            owner=owner,
            origin=OperationRunOrigin.DIRECT,
            connector_id=connector_id,
            op_id=op_id,
            target_name=_target_name(arguments.get("target")),
            params_hash=params_hash,
            approval_request_id=None,
        )
        self._launch(
            run_id,
            op_id=op_id,
            owner=owner,
            coro_factory=lambda: call_operation(operator, arguments),
        )
        return run_id

    async def submit_approval_resume(
        self,
        operator: Operator,
        request: ApprovalRequest,
        *,
        params: dict[str, Any] | None,
    ) -> uuid.UUID:
        """Create a durable run for an async approval resume and launch it.

        The approve route has already committed the decision; this hands the
        re-dispatch to the background substrate so ``/approve`` returns
        promptly with the handle instead of blocking for the resumed op's
        full duration. The resumed dispatch runs through the shared
        :func:`~meho_backplane.operations.approval_queue.resume_dispatch_after_approval`
        (same policy bypass, same exactly-one-resumer claim, same audit) so
        the async path preserves every guarantee the inline path has.

        *request* is a (detached) row; ``expire_on_commit=False`` on the
        process sessionmaker keeps its loaded columns readable in the
        background task.
        """
        from meho_backplane.operations._validate import compute_params_hash
        from meho_backplane.operations.approval_queue import resume_dispatch_after_approval

        effective_params = params if params is not None else request.params
        params_hash = (
            compute_params_hash(effective_params)
            if isinstance(effective_params, dict) and effective_params
            else None
        )
        owner = _lease_owner()
        run_id = await self._create_and_claim(
            operator,
            owner=owner,
            origin=OperationRunOrigin.APPROVAL_RESUME,
            connector_id=str(request.connector_id),
            op_id=str(request.op_id),
            target_name=None,
            params_hash=params_hash,
            approval_request_id=request.id,
        )
        self._launch(
            run_id,
            op_id=str(request.op_id),
            owner=owner,
            coro_factory=lambda: resume_dispatch_after_approval(
                operator=operator, request=request, params=params
            ),
        )
        return run_id

    async def _create_and_claim(
        self,
        operator: Operator,
        *,
        owner: str,
        origin: OperationRunOrigin,
        connector_id: str,
        op_id: str,
        target_name: str | None,
        params_hash: str | None,
        approval_request_id: uuid.UUID | None,
    ) -> uuid.UUID:
        """Insert a ``pending`` row + claim this worker's lease; commit; return id."""
        settings = get_settings()
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            row = await run_lifecycle.create_run(
                session,
                tenant_id=operator.tenant_id,
                identity_sub=operator.sub,
                origin=origin,
                connector_id=connector_id,
                op_id=op_id,
                target_name=target_name,
                params_hash=params_hash,
                approval_request_id=approval_request_id,
            )
            await run_lifecycle.claim_lease(
                session,
                row,
                owner=owner,
                ttl_seconds=settings.operation_run_lease_ttl_seconds,
            )
            await session.commit()
            return row.id

    # -- background execution --------------------------------------------

    def _launch(
        self,
        run_id: uuid.UUID,
        *,
        op_id: str,
        owner: str,
        coro_factory: _DispatchCoro,
    ) -> asyncio.Task[None]:
        """Launch the dispatch task, anchored in the store; return its handle."""
        task = asyncio.create_task(
            self._run_to_completion(run_id, op_id=op_id, owner=owner, coro_factory=coro_factory),
            name=f"operation-run-{run_id}",
        )
        self._store[run_id] = _RunState(task=task)

        def _evict(_task: asyncio.Task[None], rid: uuid.UUID = run_id) -> None:
            self._store.pop(rid, None)

        task.add_done_callback(_evict)
        return task

    async def _run_to_completion(
        self,
        run_id: uuid.UUID,
        *,
        op_id: str,
        owner: str,
        coro_factory: _DispatchCoro,
    ) -> None:
        """Background coroutine: mark running, dispatch, persist terminal state.

        Never re-raises (a failed run is a recorded ``failed`` row, not a
        crashed task): an unhandled task exception would surface only as a
        GC-time warning. A heartbeat sidecar keeps the lease fresh so the
        reaper does not reclaim a healthy long-running worker; if *this* task
        dies, the sidecar dies with it, the lease lapses, and the reaper
        drives the row to ``failed``.
        """
        structlog.contextvars.bind_contextvars(audit_op_id=op_id, audit_op_class="write")
        heartbeat = asyncio.create_task(
            self._heartbeat_loop(run_id, owner),
            name=f"operation-run-heartbeat-{run_id}",
        )
        try:
            if not await self._mark_running(run_id):
                # The run was cancelled before it started; do not dispatch.
                return
            try:
                envelope = await coro_factory()
            except asyncio.CancelledError:
                # Cooperative cancellation (shutdown / GC); leave the row for
                # the reaper to reclaim rather than writing a terminal state
                # from a torn task.
                raise
            except Exception as exc:
                _log.warning(
                    "operation_run_dispatch_unexpected_failure",
                    run_id=str(run_id),
                    op_id=op_id,
                    error=str(exc),
                )
                await self._finalize_failure(run_id, error=f"{type(exc).__name__}: {exc}")
                return
            await self._finalize_success(run_id, result=_normalize_envelope(envelope))
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            structlog.contextvars.unbind_contextvars("audit_op_id", "audit_op_class")

    async def _mark_running(self, run_id: uuid.UUID) -> bool:
        """Transition ``pending`` -> ``running``. Return ``False`` if not pending.

        A cancel that raced in before the task started leaves the row
        terminal (``cancelled``); the transition would be illegal, so we
        report ``False`` and the caller skips the dispatch entirely -- the op
        is never sent for a run the operator already cancelled.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            row = await run_lifecycle.get_run(session, run_id)
            if row is None:
                return False
            if row.status != OperationRunStatus.PENDING.value:
                _log.info(
                    "operation_run_not_started_non_pending",
                    run_id=str(run_id),
                    status=row.status,
                )
                return False
            await run_lifecycle.mark_running(session, row)
            await session.commit()
            return True

    async def _finalize_success(self, run_id: uuid.UUID, *, result: dict[str, Any]) -> None:
        """Persist the result envelope + transition ``running`` -> ``succeeded``.

        A cancel that landed while the dispatch was in flight leaves the row
        ``cancelled`` (terminal); the transition is then illegal. That is the
        best-effort cancel contract: the dispatch may have completed anyway,
        but its own synchronous audit row is the durable record of what
        executed, so discarding the envelope here loses nothing auditable.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            row = await run_lifecycle.get_run(session, run_id)
            if row is None:
                return
            try:
                await run_lifecycle.succeed_run(session, row, result=result)
                await session.commit()
            except run_lifecycle.IllegalOperationRunTransitionError:
                # The row raced to a terminal state (operator cancel) between
                # the get and the transition. ``succeed_run`` raises from its
                # Python-side guard *before* any DB write, so there is nothing
                # to undo -- the session's uncommitted changes (the in-memory
                # ``result`` assignment) are discarded when the context
                # manager closes the session.
                _log.info(
                    "operation_run_finalize_success_skipped_terminal",
                    run_id=str(run_id),
                    status=row.status,
                )

    async def _finalize_failure(self, run_id: uuid.UUID, *, error: str) -> None:
        """Record a run-crash reason + transition ``running`` -> ``failed``."""
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            row = await run_lifecycle.get_run(session, run_id)
            if row is None:
                return
            try:
                await run_lifecycle.fail_run(session, row, error=error)
                await session.commit()
            except run_lifecycle.IllegalOperationRunTransitionError:
                # Raced to terminal; the guard raised before any DB write, so
                # the closing context manager discards the uncommitted change.
                _log.info(
                    "operation_run_finalize_failure_skipped_terminal",
                    run_id=str(run_id),
                    status=row.status,
                )

    async def _heartbeat_loop(self, run_id: uuid.UUID, owner: str) -> None:
        """Extend *run_id*'s lease every ``ttl/2`` until cancelled or lost.

        A :class:`OperationRunLeaseLostError` means the reaper reclaimed the
        row (or an operator cancelled it): stop heartbeating -- the run is no
        longer ours to keep alive. Each beat opens its own short-lived
        committed transaction (the conditional ``UPDATE`` is atomic).
        """
        ttl_seconds = get_settings().operation_run_lease_ttl_seconds
        interval = max(1.0, ttl_seconds / 2.0)
        sessionmaker = get_sessionmaker()
        while True:
            await asyncio.sleep(interval)
            try:
                async with sessionmaker() as session:
                    await run_lifecycle.heartbeat(
                        session,
                        run_id=run_id,
                        owner=owner,
                        ttl_seconds=ttl_seconds,
                    )
                    await session.commit()
            except run_lifecycle.OperationRunLeaseLostError:
                _log.info("operation_run_lease_lost", run_id=str(run_id), owner=owner)
                return

    # -- read + cancel ----------------------------------------------------

    async def poll(self, operator: Operator, run_id: uuid.UUID) -> OperationRun:
        """Return the durable state of a run the operator's tenant owns.

        Reads the ``operation_run`` row (the source of truth), so it works
        after the creating request returned and even after the in-memory task
        is gone. A cross-tenant / unknown id raises
        :class:`~meho_backplane.operations.operation_run.OperationRunNotFoundError`
        -- existence is not leaked across tenants.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            row = await run_lifecycle.get_run(session, run_id)
            if row is None or row.tenant_id != operator.tenant_id:
                raise run_lifecycle.OperationRunNotFoundError(run_id)
            return row

    async def list(
        self,
        operator: Operator,
        *,
        status: OperationRunStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[OperationRun]:
        """List the operator's tenant's runs, newest first (tenant-isolated)."""
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            return await run_lifecycle.list_runs(
                session,
                tenant_id=operator.tenant_id,
                status=status,
                limit=limit,
                offset=offset,
            )

    async def cancel(self, operator: Operator, run_id: uuid.UUID) -> OperationRun:
        """Cancel a non-terminal run the operator's tenant owns.

        Records the durable cancel intent via the shared
        :func:`~meho_backplane.operations.operation_run.cancel_run` service
        path. Tenant isolation is enforced here first (a cross-tenant /
        unknown handle raises ``OperationRunNotFoundError`` -> 404), matching
        :meth:`poll`. The in-flight task is not torn down synchronously: it
        loses its lease on the next heartbeat and its result is discarded
        when it finalises against the now-terminal row (best-effort cancel).
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            row = await run_lifecycle.get_run(session, run_id)
            if row is None or row.tenant_id != operator.tenant_id:
                raise run_lifecycle.OperationRunNotFoundError(run_id)
            cancelled = await run_lifecycle.cancel_run(session, run_id, operator=operator)
            await session.commit()
            return cancelled


#: Process-wide singleton. The in-process task store must be shared across
#: every request handler in the process, so the service is a module
#: singleton (the same pattern the agent invoker uses).
_service: OperationRunService | None = None


def get_operation_run_service() -> OperationRunService:
    """Return the process-wide :class:`OperationRunService` singleton."""
    global _service
    if _service is None:
        _service = OperationRunService()
    return _service
