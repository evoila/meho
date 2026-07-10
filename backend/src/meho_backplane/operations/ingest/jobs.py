# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""In-memory job registry for asynchronous OpenAPI ingest runs (G0.16-T1).

The ingest pipeline (parser + register + LLM grouping) blocks the event
loop long enough on real-world vendor specs that the kubelet's liveness
probe times out and the pod is graceful-killed -- a 7.5 MB / 1275-op
``vmware/9.0.0.0`` spec took 30 s of CPU + DB time inline on the
request thread before the probe deadline expired
(see ``claude-rdc-hetzner-dc#771`` Finding 20, and
``docs/codebase/api-shape-conventions.md`` §1 for the strategic
"OpenAPI is the escape hatch, not the daily-driver" framing).

This module is the off-the-request-thread shape: the route hands the
ingest work off to an ``asyncio.create_task`` background coroutine and
returns ``202 Accepted`` + a job-id; operators poll
``GET /api/v1/connectors/ingest/jobs/{job_id}`` for status and result.

Storage is **process-local**. The backplane deployment runs one pod per
namespace (helm ``meho-chart`` default), and a pod restart blows the
in-memory state away on purpose -- a job whose pod died was never going
to finish anyway. Durable jobs across restarts is a v0.9 follow-up
(see "Known issues" in ``docs/codebase/spec-ingestion.md``); the SEV-3
"don't crash the pod" framing this task closes is satisfied by the
fire-and-forget shape alone.

Tenant scope: every job row carries the originating operator's
``tenant_id`` (``None`` for built-in scope). The polling endpoint
applies the same cross-tenant 404 conflation
:class:`~meho_backplane.operations.ingest.ReviewService` uses -- an
operator cannot enumerate other tenants by status-code differential.

The registry is bounded (FIFO eviction past
:data:`_MAX_JOBS_RETAINED`) so a long-running pod that handles many
ingest calls doesn't grow unbounded. Eviction is bounded-recency,
not bounded-time, because the operator workflow is "run ingest, watch
the job, page on failure" -- a few hundred terminal jobs is more than
enough scrollback.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Final, Literal
from uuid import UUID

import structlog

from meho_backplane.operations.ingest.pipeline import IngestionPipelineResult

__all__ = [
    "IngestJob",
    "IngestJobNotFoundError",
    "IngestJobRegistry",
    "IngestJobStatus",
    "get_job_registry",
    "reset_job_registry_for_tests",
]

_log = structlog.get_logger(__name__)

#: Bounded retention cap on terminal job rows. Past this count the
#: registry evicts oldest-first by completion time. 256 covers
#: weeks of operator escape-hatch use at the expected cadence
#: (~handful of ingests per release window) while keeping the
#: per-pod memory footprint trivial.
_MAX_JOBS_RETAINED = 256

#: Fallback wall-clock watchdog budget (seconds) for a single async
#: ingest job. Matches the Anthropic-family agent-approval wait default
#: (30 min): comfortably longer than a legitimate large-spec run (a
#: 7.5 MB / 1275-op vmware spec is tens of seconds of CPU + DB) yet an
#: order of magnitude below the ~30 min wall-clock a hung grouping call
#: could otherwise reach on the Anthropic SDK's default 10-min-read,
#: 2-retry ceiling. Past this budget the job body is cancelled and the
#: row flips to ``failed`` (``error_class="TimeoutError"``) rather than
#: sitting at ``running`` until a pod restart clears it.
_DEFAULT_INGEST_JOB_TIMEOUT_SECONDS: Final[float] = 1800.0


def _load_ingest_job_timeout_seconds() -> float:
    """Read the watchdog budget from ``INGEST_JOB_TIMEOUT_SECONDS``.

    Env-overridable (a slow shared executor / very large fleet of specs
    may want a longer ceiling) with a sane default. Parsed defensively —
    a malformed or non-positive value falls back to
    :data:`_DEFAULT_INGEST_JOB_TIMEOUT_SECONDS` with a warning rather
    than crashing the ingest package at import, because a timeout typo
    should not take the whole backplane down on boot.
    """
    raw = os.environ.get("INGEST_JOB_TIMEOUT_SECONDS")
    if raw is None:
        return _DEFAULT_INGEST_JOB_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        _log.warning(
            "ingest_job_timeout_env_invalid",
            raw=raw,
            fallback_seconds=_DEFAULT_INGEST_JOB_TIMEOUT_SECONDS,
        )
        return _DEFAULT_INGEST_JOB_TIMEOUT_SECONDS
    if value <= 0:
        _log.warning(
            "ingest_job_timeout_env_non_positive",
            value=value,
            fallback_seconds=_DEFAULT_INGEST_JOB_TIMEOUT_SECONDS,
        )
        return _DEFAULT_INGEST_JOB_TIMEOUT_SECONDS
    return value


#: Resolved watchdog budget, read once at import from the environment.
#: ``run_ingest_job`` reads this module global at call time (not as a
#: default-arg snapshot) so a test can shrink it via the ``timeout_s``
#: parameter without monkeypatching import-time state.
_INGEST_JOB_TIMEOUT_SECONDS: float = _load_ingest_job_timeout_seconds()

#: Snapshot of an ingest run's lifecycle. The status enum lives at
#: module scope so the route layer + tests share one source of truth
#: rather than re-declaring the literal in each Pydantic projection.
#:
#: ``degraded`` (G0.24 / claude-rdc-hetzner-dc#1136) is a terminal state
#: distinct from ``failed``: the pipeline coroutine returned without
#: raising, but a postcondition found nothing dispatchable was persisted
#: (zero inserts, or rows that are invisible to the dispatch/query
#: surface). A bare ``succeeded`` there is a lie — the catalog row reads
#: ``registered, 0 ops`` immediately after — so the job ends ``degraded``
#: carrying a structured ``error_class`` while still surfacing the
#: ingestion counts so the operator sees what (didn't) land.
IngestJobStatus = Literal["running", "succeeded", "failed", "degraded"]


class IngestJobNotFoundError(LookupError):
    """Raised when :meth:`IngestJobRegistry.get` cannot find a job_id.

    Inherits :class:`LookupError` rather than :class:`KeyError` so the
    REST router can ``except IngestJobNotFoundError`` without
    accidentally swallowing dict-key lookups elsewhere in the request
    handler. The route maps this onto HTTP 404 with the cross-tenant
    404 conflation
    :class:`~meho_backplane.operations.ingest.ReviewService` uses --
    operators cannot enumerate other tenants by status-code differential.
    """


@dataclass
class IngestJob:
    """One ingest pipeline run, fired-and-forgotten off the request thread.

    Carries the operator-supplied identifiers (so the polling endpoint
    can render them without a separate lookup), the originating
    tenant scope (load-bearing for the tenant-isolation gate), the
    request-shape descriptors (``catalog_entry`` / ``product`` /
    ``version`` / ``impl_id`` -- the polling response echoes whichever
    fields the operator supplied), wall-clock timing, and on
    completion the pipeline's structured result (or the failure's
    exception class + message).

    A live job has ``status="running"`` and ``ended_at is None``.
    Terminal jobs carry one of ``status="succeeded"`` (with
    ``result`` populated), ``status="failed"`` (with ``error`` and
    ``error_class`` populated, no ``result`` — the pipeline raised), or
    ``status="degraded"`` (the pipeline returned but its output was not
    dispatchable: ``result`` *and* ``error`` / ``error_class`` are both
    populated so the operator sees the counts that landed alongside the
    structured reason they're not usable). The route projects the right
    subset of fields per status into the polling response.

    The dataclass is mutable on purpose -- the background coroutine
    flips ``status`` / ``ended_at`` / ``result`` / ``error`` on
    completion under the registry's lock.
    """

    job_id: UUID
    tenant_id: UUID | None
    operator_sub: str
    status: IngestJobStatus = "running"
    catalog_entry: str | None = None
    product: str | None = None
    version: str | None = None
    impl_id: str | None = None
    spec_uris: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    result: IngestionPipelineResult | None = None
    error: str | None = None
    error_class: str | None = None


class IngestJobRegistry:
    """Process-local registry of in-flight + terminal ingest jobs.

    The registry is a thin wrapper around an :class:`OrderedDict`
    behind an :class:`asyncio.Lock`. Every mutating call site
    (create, complete, fail) takes the lock; reads (get, list) take
    it too because :class:`OrderedDict.move_to_end` mutates structure
    on the LRU bookkeeping path.

    Bounded eviction (oldest terminal job first) runs on create so
    a steady stream of ingests can't push pod memory unbounded. Live
    jobs are exempt from eviction -- the registry never removes a
    ``running`` row out from under its background task.

    Single registry per process. The module-level :func:`get_job_registry`
    accessor returns the same instance every call; tests reset via
    :func:`reset_job_registry_for_tests`.
    """

    def __init__(self) -> None:
        self._jobs: OrderedDict[UUID, IngestJob] = OrderedDict()
        self._lock = asyncio.Lock()

    async def create(
        self,
        *,
        operator_sub: str,
        tenant_id: UUID | None,
        catalog_entry: str | None,
        product: str | None,
        version: str | None,
        impl_id: str | None,
        spec_uris: list[str],
    ) -> IngestJob:
        """Insert a ``running`` job row and return it.

        Generates a fresh :class:`uuid.UUID` for the job id (random
        UUID4 so cross-pod log-correlation reads are unambiguous;
        the natural alternative -- ``operator_sub + started_at`` --
        would collide on rapid retries from the same operator).

        Evicts oldest terminal jobs past :data:`_MAX_JOBS_RETAINED`
        before insertion so the registry stays bounded; running jobs
        are exempt from the eviction pass.
        """
        async with self._lock:
            self._evict_oldest_terminal_locked()
            job = IngestJob(
                job_id=uuid.uuid4(),
                tenant_id=tenant_id,
                operator_sub=operator_sub,
                catalog_entry=catalog_entry,
                product=product,
                version=version,
                impl_id=impl_id,
                spec_uris=list(spec_uris),
            )
            self._jobs[job.job_id] = job
            return job

    async def complete(
        self,
        job_id: UUID,
        *,
        result: IngestionPipelineResult,
    ) -> None:
        """Flip *job_id* to ``succeeded`` and record *result*.

        No-op when *job_id* was evicted between the background task
        launch and the completion call -- the pod restarted, or the
        registry rolled past retention. The background task does not
        treat eviction-during-run as a failure; the result is lost
        but the operator's polling already returned 404 by then.
        """
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = "succeeded"
            job.ended_at = time.time()
            job.result = result
            self._jobs.move_to_end(job_id)

    async def degrade(
        self,
        job_id: UUID,
        *,
        result: IngestionPipelineResult,
        error_class: str,
        error: str,
    ) -> None:
        """Flip *job_id* to ``degraded`` — the pipeline returned but nothing dispatchable landed.

        The pipeline coroutine completed without raising, so *result*
        carries real counts, but a postcondition (see
        :func:`run_ingest_job`) found the run produced nothing the
        dispatch/query surface can resolve. Records *result* (so the
        operator still sees inserted/skipped counts) **and** the
        structured *error_class* / *error* (so the failure is
        machine-branchable and operator-readable) — a job that lied with
        a bare ``succeeded`` is the failure mode this state closes
        (claude-rdc-hetzner-dc#1136).

        No-op when *job_id* was evicted between the background-task
        launch and this call — same eviction-during-run contract as
        :meth:`complete`.
        """
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = "degraded"
            job.ended_at = time.time()
            job.result = result
            job.error_class = error_class
            job.error = error if len(error) <= 1024 else error[:1024] + "...<truncated>"
            self._jobs.move_to_end(job_id)

    async def fail(
        self,
        job_id: UUID,
        *,
        error: BaseException,
    ) -> None:
        """Flip *job_id* to ``failed`` and record the exception summary.

        Records the exception class name (so operators / agents can
        branch on the diagnostic without re-parsing the prose) plus
        ``str(error)`` capped at a sensible length. The
        :class:`HTTPException` shape the synchronous path used to
        surface for operator-facing errors is **lost** by this code
        path -- the background task doesn't have a route context to
        raise into. The polling response therefore renders
        ``error_class`` + ``error`` as plain strings; the structured
        ``detail`` body the synchronous shape returned no longer fits.
        Operators reaching for the escape hatch trade the structured
        422 shape for the don't-crash-the-pod guarantee. The trade-off
        is documented at the route layer.
        """
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = "failed"
            job.ended_at = time.time()
            job.error_class = type(error).__name__
            message = str(error)
            job.error = message if len(message) <= 1024 else message[:1024] + "...<truncated>"
            self._jobs.move_to_end(job_id)

    async def get(
        self,
        job_id: UUID,
        *,
        tenant_id: UUID | None,
        is_tenant_admin: bool,
    ) -> IngestJob:
        """Return the job for *job_id* scoped to *tenant_id*.

        Cross-tenant probes raise :class:`IngestJobNotFoundError`
        rather than a permission error -- same conflation
        :class:`ReviewService` uses to keep the operator-facing
        failure surface uniform. Built-in scope (``tenant_id=None``)
        is readable only by ``tenant_admin``; non-admin operators
        asking about a built-in job see the same 404 as a missing id.

        Updates the LRU position on every read so an in-flight job
        the operator is actively polling doesn't get evicted out
        from under them by a burst of newer creates.
        """
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise IngestJobNotFoundError(str(job_id))
            if job.tenant_id is None and not is_tenant_admin:
                raise IngestJobNotFoundError(str(job_id))
            if job.tenant_id is not None and job.tenant_id != tenant_id:
                raise IngestJobNotFoundError(str(job_id))
            self._jobs.move_to_end(job_id)
            return job

    def _evict_oldest_terminal_locked(self) -> None:
        """Trim terminal jobs past :data:`_MAX_JOBS_RETAINED`.

        Lock-held variant; callers acquire :attr:`_lock` first.
        Walks oldest-first and removes the first non-running row;
        running jobs are skipped entirely so the bookkeeping never
        evicts a row whose background task is still going to flip it.
        """
        while len(self._jobs) >= _MAX_JOBS_RETAINED:
            evicted = False
            for candidate_id, candidate in list(self._jobs.items()):
                if candidate.status != "running":
                    self._jobs.pop(candidate_id)
                    evicted = True
                    break
            if not evicted:
                # Every retained row is still running -- nothing to evict.
                # The registry is allowed to grow past the cap until one
                # of those rows completes; pathological growth is bounded
                # by the deployment's concurrency limit, not this counter.
                return


#: Module-level singleton accessor. Production callers use this; tests
#: reset between runs via :func:`reset_job_registry_for_tests`.
_registry: IngestJobRegistry = IngestJobRegistry()


def get_job_registry() -> IngestJobRegistry:
    """Return the process-wide :class:`IngestJobRegistry`.

    Accessor rather than a direct ``_registry`` import so a test that
    re-points the registry via :func:`reset_job_registry_for_tests`
    affects every consumer in lock-step.
    """
    return _registry


def reset_job_registry_for_tests() -> None:
    """Replace the module-level registry with a fresh instance.

    Test seam only. Production code never calls this; pre-commit /
    runtime stay on the singleton created at import time.
    """
    global _registry
    _registry = IngestJobRegistry()


#: Structured ``error_class`` an async ingest carries when it ran to
#: completion but left the connector non-dispatchable. The CLI / agent
#: branch on this string to tell "the pipeline raised" (``failed`` with
#: the exception class) apart from "the pipeline returned but nothing
#: usable landed" (``degraded`` with this class). claude-rdc-hetzner-dc#1136.
INGESTED_NOT_DISPATCHABLE = "ingested_not_dispatchable"

#: Post-run dispatchability probe injected by the route. Given the
#: pipeline's :class:`IngestionPipelineResult`, returns whether the
#: connector it produced is resolvable by the dispatch/query surface
#: (``connector_exists`` under the parser-derived natural key, scoped to
#: the originating tenant). A closure (not an inline DB call) so
#: :func:`run_ingest_job` stays free of a session/tenant dependency and
#: tests can inject a deterministic verdict; the route closes over the
#: operator's ``tenant_id`` + ``connector_exists`` to build the real one.
DispatchabilityCheck = Callable[[IngestionPipelineResult], Awaitable[bool]]


async def run_ingest_job(
    job_id: UUID,
    *,
    pipeline_call: Callable[[], Awaitable[IngestionPipelineResult]],
    registry: IngestJobRegistry | None = None,
    dispatchability_check: DispatchabilityCheck | None = None,
    timeout_s: float | None = None,
) -> None:
    """Drive *pipeline_call* off the request thread and reconcile the job row.

    Background-coroutine body for the fire-and-forget shape the
    ``POST /api/v1/connectors/ingest`` route (and the MCP ingest tool)
    use: the caller builds the pipeline closure, kicks this off with
    :func:`asyncio.create_task`, and immediately returns 202 + a job
    handle. This helper reconciles the run into the in-memory job row as
    ``succeeded`` / ``degraded`` (see :func:`_reconcile_returned_result`)
    or ``failed`` -- the latter on any exception, including the watchdog's
    ``TimeoutError``. The exception is swallowed (no re-raise) except
    ``SystemExit`` / ``KeyboardInterrupt``: the 202 already went out, so
    re-raising would only log a noisy traceback for a routine
    operator-facing failure.

    Terminal-state guarantee (#2275): an exception handler alone does not
    cover a *never-completing await* -- a starved ``to_thread`` executor,
    a DB acquire that never returns, or a grouping LLM call pending on the
    SDK's default ~30-min ceiling -- which would strand the job at
    ``running`` until a pod restart clears the in-memory registry. The
    whole body (the pipeline call **and** the post-run dispatchability
    probe, whose own hang would otherwise strand the job) runs inside
    ``asyncio.timeout``, budget *timeout_s* or the module default
    :data:`_INGEST_JOB_TIMEOUT_SECONDS` (env
    ``INGEST_JOB_TIMEOUT_SECONDS``). At the deadline the body is cancelled
    and re-raised as ``TimeoutError`` -> ``failed``. The guarantee is
    job-state terminality, not thread reclamation: an already-running
    ``to_thread`` OS thread cannot be cancelled and may linger.

    ``ingest_job_id`` is bound into structlog *contextvars* for the run so
    the configured ``merge_contextvars`` processor stamps it onto every
    event emitted under the pipeline call -- the pipeline binds only its
    own ``connector_id`` logger, so a plain ``_log.bind`` here would leave
    a job-id-filtered grep blind to every pipeline event. See
    ``docs/codebase/spec-ingestion.md`` for the full narrative.
    """
    if registry is None:
        registry = get_job_registry()
    timeout_seconds = _INGEST_JOB_TIMEOUT_SECONDS if timeout_s is None else timeout_s
    with structlog.contextvars.bound_contextvars(ingest_job_id=str(job_id)):
        try:
            # Watchdog: time-box the whole body -- the pipeline call AND
            # the post-run dispatchability probe (a real DB read whose own
            # hang would strand the job) -- so the row always reaches a
            # terminal state. ``asyncio.timeout`` cancels a wedged await at
            # the deadline and re-raises it as ``TimeoutError``, which the
            # ``except`` below routes to ``failed``.
            async with asyncio.timeout(timeout_seconds):
                result = await pipeline_call()
                await _reconcile_returned_result(
                    job_id,
                    result=result,
                    registry=registry,
                    dispatchability_check=dispatchability_check,
                )
        except BaseException as exc:
            _log.info(
                "ingest_job_failed",
                error_class=type(exc).__name__,
                error_message=str(exc)[:512],
            )
            await registry.fail(job_id, error=exc)
            # Re-raise system-exit / keyboard-interrupt class exceptions
            # so they aren't silently swallowed -- the rest (including the
            # watchdog ``TimeoutError``) are operator-facing failure modes
            # routed via the registry above.
            if isinstance(exc, (SystemExit, KeyboardInterrupt)):
                raise
            return


async def _reconcile_returned_result(
    job_id: UUID,
    *,
    result: IngestionPipelineResult,
    registry: IngestJobRegistry,
    dispatchability_check: DispatchabilityCheck | None,
) -> None:
    """Flip a *returned* pipeline result to ``succeeded`` or ``degraded``.

    Applies the dispatchability postcondition
    (claude-rdc-hetzner-dc#1136): a coroutine that returned without
    raising is **not** sufficient evidence the ingest is dispatchable --
    an async ``--spec`` run can persist rows under a product key the
    dispatch surface never queries (the VCF-family long<->short split,
    reconciled at register-time) or skip every op against a prior run,
    leaving the catalog row ``registered, 0 ops``. The job ends
    ``succeeded`` only when ``inserted_count > 0`` **and**
    *dispatchability_check* resolves the connector (or a benign idempotent
    zero-insert re-run); otherwise ``degraded`` carrying
    ``error_class="ingested_not_dispatchable"`` and the counts that (did
    not) land. *dispatchability_check* is ``None`` only for legacy callers
    / tests that opt out; the production route always supplies one.
    """
    degraded_reason = await _dispatchability_failure_reason(
        result=result,
        dispatchability_check=dispatchability_check,
    )
    if degraded_reason is not None:
        _log.warning(
            "ingest_job_degraded",
            connector_id=result.connector_id,
            inserted_count=result.ingestion.inserted_count,
            error_class=INGESTED_NOT_DISPATCHABLE,
            reason=degraded_reason,
        )
        await registry.degrade(
            job_id,
            result=result,
            error_class=INGESTED_NOT_DISPATCHABLE,
            error=degraded_reason,
        )
        return
    _log.info(
        "ingest_job_succeeded",
        connector_id=result.connector_id,
        inserted_count=result.ingestion.inserted_count,
        groups_created=result.grouping.groups_created if result.grouping else None,
    )
    await registry.complete(job_id, result=result)


async def _dispatchability_failure_reason(
    *,
    result: IngestionPipelineResult,
    dispatchability_check: DispatchabilityCheck | None,
) -> str | None:
    """Return an operator-facing reason the run is non-dispatchable, or ``None`` if it is.

    Two failure modes collapse to the one ``ingested_not_dispatchable``
    ``error_class`` but carry distinct messages so the operator knows
    which one fired:

    * **Nothing persisted** — ``inserted_count == 0``. The run touched no
      new rows. Two shapes collapse here and the probe is what tells them
      apart: an empty/first-run spec that yielded nothing dispatchable
      (degraded), versus a **benign idempotent re-run** where every op was
      ``skipped`` because a prior run already persisted them dispatchably
      (succeeded). ``_upsert.upsert_one_operation`` returns ``"skipped"``
      for an unchanged op, so a re-ingest of an already-dispatchable
      connector lands ``inserted_count == 0`` on a perfectly healthy
      connector — degrading it unconditionally would flip a no-op re-run
      to a non-zero CLI failure. The probe (when supplied) breaks the tie:
      already-dispatchable ⇒ ``succeeded``, else ⇒ degraded.
    * **Persisted-but-invisible** — ``inserted_count > 0`` yet
      *dispatchability_check* returns ``False``: rows landed under a key
      the dispatch/query surface does not resolve. This is the exact
      claude-rdc-hetzner-dc#1136 mis-keyed-product symptom (pre the
      register-time reconciliation) and any future drift of the same
      shape.

    *dispatchability_check* ``None`` (legacy caller / opt-out) treats a
    non-empty insert as dispatchable and — having no way to tell an
    idempotent re-run from a genuinely empty first run apart — degrades a
    zero-insert run. An opted-out caller has accepted both gaps.
    """
    # The probe is the only authority on whether the connector resolves
    # under its dispatch key, so run it once (when supplied) and let both
    # the zero-insert and the persisted-but-invisible branches read its
    # verdict. Fail open: a probe that *raises* (e.g. a transient DB error
    # on the connector_exists read) must not strand the job in ``running``
    # or degrade it on a false signal — the pipeline DID complete.
    dispatchable: bool | None = None
    if dispatchability_check is not None:
        try:
            dispatchable = await dispatchability_check(result)
        except Exception:
            _log.warning(
                "ingest_job_dispatchability_probe_failed",
                connector_id=result.connector_id,
                inserted_count=result.ingestion.inserted_count,
                exc_info=True,
            )
            return None

    if result.ingestion.inserted_count == 0:
        # A re-ingest of an already-persisted spec skips every op, so
        # ``inserted_count == 0`` on a perfectly healthy, already-dispatchable
        # connector (benign idempotency) — keep it ``succeeded``. Only a
        # zero-insert run that is *also* non-dispatchable (empty/first-run
        # spec, or no probe to confirm) degrades.
        if dispatchable:
            return None
        return (
            "ingest completed but persisted no new operations "
            f"(inserted_count=0) for connector_id={result.connector_id!r}; "
            "nothing is dispatchable. If a prior run already persisted "
            "these ops dispatchably this is benign idempotency, but a "
            "first run reaching zero inserts means the spec yielded no "
            "operations under this connector."
        )

    if dispatchable is False:
        return (
            f"ingest persisted {result.ingestion.inserted_count} operation(s) "
            f"but connector_id={result.connector_id!r} is not resolvable by "
            "the dispatch/query surface (search_operations / "
            "list_operation_groups return connector_not_ingested); the rows "
            "landed under a product key the dispatcher does not look them up "
            "under. Re-ingest under the --product the catalog's next_step "
            "verb prints."
        )
    return None
