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
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal
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

#: Snapshot of an ingest run's lifecycle. The status enum lives at
#: module scope so the route layer + tests share one source of truth
#: rather than re-declaring the literal in each Pydantic projection.
IngestJobStatus = Literal["running", "succeeded", "failed"]


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
    ``result`` populated) or ``status="failed"`` (with ``error`` and
    ``error_class`` populated). The route projects the right subset
    of fields per status into the polling response.

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


async def run_ingest_job(
    job_id: UUID,
    *,
    pipeline_call: Callable[[], Awaitable[IngestionPipelineResult]],
    registry: IngestJobRegistry | None = None,
) -> None:
    """Drive *pipeline_call* off the request thread and reconcile the job row.

    Background-coroutine body for the fire-and-forget shape the
    ``POST /api/v1/connectors/ingest`` route uses. The route
    constructs the pipeline-invocation closure (so this helper stays
    framework-agnostic), kicks the coroutine off with
    :func:`asyncio.create_task`, and immediately returns the 202 + job
    handle. This helper:

    1. Runs the closure under a structlog binding tagged with
       ``ingest_job_id`` so the log output during the long-running
       pass is correlatable with the polling responses.
    2. On success, flips the job to ``succeeded`` carrying the
       structured :class:`IngestionPipelineResult`.
    3. On any exception, flips the job to ``failed`` and records the
       exception class + capped message. The exception is swallowed
       (no re-raise) -- the request that started the job has already
       returned 202, and re-raising into the asyncio default exception
       handler would log a noisy traceback for what is now a routine
       operator-facing failure mode.

    The structured failure logged at :func:`structlog.get_logger`
    INFO level preserves the diagnostic for ops dashboards even
    though the exception itself is swallowed.
    """
    if registry is None:
        registry = get_job_registry()
    log = _log.bind(ingest_job_id=str(job_id))
    try:
        result = await pipeline_call()
    except BaseException as exc:
        log.info(
            "ingest_job_failed",
            error_class=type(exc).__name__,
            error_message=str(exc)[:512],
        )
        await registry.fail(job_id, error=exc)
        # Re-raise system-exit / keyboard-interrupt class exceptions
        # so they aren't silently swallowed -- the rest are
        # operator-facing failure modes routed via the registry above.
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            raise
        return
    log.info(
        "ingest_job_succeeded",
        connector_id=result.connector_id,
        inserted_count=result.ingestion.inserted_count,
        groups_created=result.grouping.groups_created if result.grouping else None,
    )
    await registry.complete(job_id, result=result)
