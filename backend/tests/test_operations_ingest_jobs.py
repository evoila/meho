# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the async-ingest job registry + the post-run honesty gate.

Covers :func:`meho_backplane.operations.ingest.jobs.run_ingest_job`'s
reconciliation of a background pipeline run into the in-memory job row,
with the dispatchability postcondition (G0.24 /
claude-rdc-hetzner-dc#1136) front and centre:

* A pipeline that returns a dispatchable result flips the job to
  ``succeeded``.
* A pipeline that returns but persisted nothing dispatchable —
  ``inserted_count == 0`` *or* the injected dispatchability probe
  returns ``False`` — flips the job to ``degraded`` carrying
  ``error_class="ingested_not_dispatchable"``, never a bare
  ``succeeded``. The ``degraded`` row still carries the pipeline's
  ``result`` so the operator sees the counts that (didn't) land.
* A pipeline that raises still flips to ``failed`` (the pre-existing
  contract is preserved).

These are pure unit tests: ``run_ingest_job`` is driven directly with a
stub ``pipeline_call`` closure and a stub ``dispatchability_check``, so
no DB or HTTP layer is exercised. The DB-backed end-to-end proof that an
async ``--product vcf-logs`` ingest is dispatchable lives in
``test_operations_register_ingested.py`` (the product-reconciliation
side of the same task).
"""

from __future__ import annotations

import asyncio
import math
import time

import pytest
import structlog
from structlog.testing import capture_logs

from meho_backplane.operations.ingest import (
    IngestionPipelineResult,
    IngestJob,
    IngestJobRegistry,
    OpIdCollision,
    jobs,
    run_ingest_job,
)
from meho_backplane.operations.ingest.jobs import INGESTED_NOT_DISPATCHABLE
from meho_backplane.operations.ingest.register_ingested import IngestionResult


@pytest.fixture(autouse=True)
def _rebind_jobs_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give each test a fresh, unrealized ``jobs._log`` structlog proxy.

    Guards against an xdist ``--dist loadscope`` ordering hazard. A sibling
    module that calls ``meho_backplane.logging.configure_logging()`` sets
    ``cache_logger_on_first_use=True`` and installs a **fresh** processors
    list; a later sibling call swaps in yet another fresh list. Once the
    module-level ``jobs._log`` proxy is realized (e.g. by an earlier
    ``run_ingest_job`` test that emits a log line) it caches a bound logger
    holding a reference to whichever list was live *then*.
    ``structlog.testing.capture_logs`` mutates the *current* config list in
    place (by design, to reach live bound loggers), so it can no longer
    intercept the orphaned cached logger — the capture comes back empty and
    ``test_load_timeout_env_rejected_values_fall_back`` (and the other
    capture-based tests here) fail deterministically depending on worker
    layout. Rebinding a fresh proxy per test forces re-realization against
    the live config list inside the test — the same list ``capture_logs``
    mutates — so the capture is robust regardless of prior realization.
    ``monkeypatch`` restores the original module logger after each test;
    product ``jobs.py`` behaviour is unchanged.
    """
    monkeypatch.setattr(jobs, "_log", structlog.get_logger(jobs.__name__))


def _pipeline_result(
    *,
    connector_id: str = "vrli-rest-9.0",
    inserted_count: int = 5,
) -> IngestionPipelineResult:
    """Build a minimal :class:`IngestionPipelineResult` for the job-row tests.

    ``grouping=None`` keeps the shape small — the honesty gate only
    reads ``connector_id`` + ``ingestion.inserted_count``.
    """
    return IngestionPipelineResult(
        connector_id=connector_id,
        ingestion=IngestionResult(
            inserted_count=inserted_count,
            updated_count=0,
            skipped_count=0,
            connector_registered=True,
            operations_grouped=False,
        ),
        grouping=None,
    )


async def _create_running_job(registry: IngestJobRegistry) -> IngestJob:
    """Insert a ``running`` job row and return it."""
    return await registry.create(
        operator_sub="op-sub",
        tenant_id=None,
        catalog_entry=None,
        product="vrli",
        version="9.0",
        impl_id="vrli-rest",
        spec_uris=["file:///vrli.yaml"],
    )


@pytest.mark.asyncio
async def test_dispatchable_run_flips_to_succeeded() -> None:
    """A non-empty, dispatchable pipeline result completes the job ``succeeded``."""
    registry = IngestJobRegistry()
    job = await _create_running_job(registry)
    result = _pipeline_result(inserted_count=5)

    async def _check(_result: IngestionPipelineResult) -> bool:
        return True

    await run_ingest_job(
        job.job_id,
        pipeline_call=lambda: _async_return(result),
        registry=registry,
        dispatchability_check=_check,
    )

    stored = await registry.get(job.job_id, tenant_id=None, is_tenant_admin=True)
    assert stored.status == "succeeded"
    assert stored.result is result
    assert stored.error is None
    assert stored.error_class is None
    assert stored.ended_at is not None


@pytest.mark.asyncio
async def test_zero_insert_first_run_degrades_not_succeeds() -> None:
    """An empty first-run (0 inserts, probe says non-dispatchable) ends ``degraded``.

    This is the false-success the gate closes: the coroutine returned
    without raising, so the pre-#1136 code flipped the job to
    ``succeeded`` even though zero operations landed under a connector the
    dispatcher cannot resolve. The job must end ``degraded`` with the
    structured ``ingested_not_dispatchable`` class. The probe IS consulted
    on the zero-insert branch (a re-run that skips every op also reaches
    ``inserted_count == 0`` but stays dispatchable — see the idempotent
    re-run case below), and here returns ``False``.
    """
    registry = IngestJobRegistry()
    job = await _create_running_job(registry)
    result = _pipeline_result(inserted_count=0)

    probe_calls = 0

    async def _check(_result: IngestionPipelineResult) -> bool:
        nonlocal probe_calls
        probe_calls += 1
        return False

    await run_ingest_job(
        job.job_id,
        pipeline_call=lambda: _async_return(result),
        registry=registry,
        dispatchability_check=_check,
    )

    stored = await registry.get(job.job_id, tenant_id=None, is_tenant_admin=True)
    assert stored.status == "degraded"
    assert stored.error_class == INGESTED_NOT_DISPATCHABLE
    assert stored.error is not None and "inserted_count=0" in stored.error
    # The counts that landed (none) still ride along for diagnosis.
    assert stored.result is result
    assert probe_calls == 1


@pytest.mark.asyncio
async def test_idempotent_zero_insert_rerun_succeeds() -> None:
    """A benign idempotent re-run (0 inserts, already dispatchable) stays ``succeeded``.

    Re-ingesting an already-persisted spec skips every op
    (``_upsert.upsert_one_operation`` returns ``"skipped"``), so
    ``inserted_count == 0`` on a perfectly healthy, already-dispatchable
    connector. Degrading it unconditionally (the pre-fix behaviour) flipped
    a no-op re-run into a non-zero CLI failure; consulting the probe keeps
    it ``succeeded`` because the connector still resolves under its dispatch
    key.
    """
    registry = IngestJobRegistry()
    job = await _create_running_job(registry)
    result = _pipeline_result(inserted_count=0)

    async def _check_dispatchable(_result: IngestionPipelineResult) -> bool:
        return True

    await run_ingest_job(
        job.job_id,
        pipeline_call=lambda: _async_return(result),
        registry=registry,
        dispatchability_check=_check_dispatchable,
    )

    stored = await registry.get(job.job_id, tenant_id=None, is_tenant_admin=True)
    assert stored.status == "succeeded"
    assert stored.error is None
    assert stored.error_class is None
    assert stored.result is result


@pytest.mark.asyncio
async def test_persisted_but_invisible_run_degrades() -> None:
    """Rows persisted but unresolvable by the dispatch surface ⇒ ``degraded``.

    The exact claude-rdc-hetzner-dc#1136 mis-keyed-product symptom:
    ``inserted_count > 0`` yet the dispatch/query probe
    (``connector_exists`` under the parser-derived key) returns
    ``False``. The job must end ``degraded`` carrying the structured
    class and a message that names the persisted-but-invisible failure
    mode, while still surfacing the insert count.
    """
    registry = IngestJobRegistry()
    job = await _create_running_job(registry)
    result = _pipeline_result(connector_id="vrli-rest-9.0", inserted_count=7)

    async def _check_invisible(_result: IngestionPipelineResult) -> bool:
        return False

    await run_ingest_job(
        job.job_id,
        pipeline_call=lambda: _async_return(result),
        registry=registry,
        dispatchability_check=_check_invisible,
    )

    stored = await registry.get(job.job_id, tenant_id=None, is_tenant_admin=True)
    assert stored.status == "degraded"
    assert stored.error_class == INGESTED_NOT_DISPATCHABLE
    assert stored.error is not None
    assert "not resolvable" in stored.error
    assert "7 operation" in stored.error
    assert stored.result is result


@pytest.mark.asyncio
async def test_raising_pipeline_still_fails() -> None:
    """A pipeline that raises flips to ``failed`` — the pre-existing contract holds.

    The honesty gate only governs the *returned-but-non-dispatchable*
    case; an exception is still a hard ``failed`` with the exception
    class recorded, and the dispatchability probe is never consulted
    (there is no result to probe).
    """
    registry = IngestJobRegistry()
    job = await _create_running_job(registry)

    probe_calls = 0

    async def _check(_result: IngestionPipelineResult) -> bool:
        nonlocal probe_calls
        probe_calls += 1
        return True

    async def _raise() -> IngestionPipelineResult:
        raise ValueError("spec parse blew up")

    await run_ingest_job(
        job.job_id,
        pipeline_call=_raise,
        registry=registry,
        dispatchability_check=_check,
    )

    stored = await registry.get(job.job_id, tenant_id=None, is_tenant_admin=True)
    assert stored.status == "failed"
    assert stored.error_class == "ValueError"
    assert stored.error is not None and "spec parse blew up" in stored.error
    assert stored.result is None
    assert probe_calls == 0


@pytest.mark.asyncio
async def test_wedged_pipeline_times_out_to_failed() -> None:
    """A never-returning ``pipeline_call`` is watchdog-failed, not left ``running``.

    The terminal-state guarantee (#2275): an await that never resolves
    (a starved ``to_thread`` executor, a hung DB acquire, a grouping LLM
    call pending on the SDK's ~30-min ceiling) must not strand the job at
    ``running`` until a pod restart clears the in-memory registry. With a
    test-shrunk budget the job flips to ``failed`` carrying
    ``error_class="TimeoutError"`` well within the budget.
    """
    registry = IngestJobRegistry()
    job = await _create_running_job(registry)

    async def _never_returns() -> IngestionPipelineResult:
        await asyncio.Event().wait()  # blocks forever until cancelled
        raise AssertionError("unreachable")  # pragma: no cover

    started = time.monotonic()
    await run_ingest_job(
        job.job_id,
        pipeline_call=_never_returns,
        registry=registry,
        timeout_s=0.05,
    )
    elapsed = time.monotonic() - started

    stored = await registry.get(job.job_id, tenant_id=None, is_tenant_admin=True)
    assert stored.status == "failed"
    assert stored.error_class == "TimeoutError"
    assert stored.ended_at is not None
    assert stored.result is None
    # Terminated promptly on the shrunk budget -- nowhere near the 30-min default.
    assert elapsed < 5.0


@pytest.mark.asyncio
async def test_wedged_dispatchability_probe_times_out_to_failed() -> None:
    """A hang in the *post-run* dispatchability probe also terminates the job.

    The watchdog wraps the whole body, not just ``pipeline_call``: the
    dispatchability probe is a real DB read (``connector_exists``) whose
    own hang would strand the job if only the pipeline call were guarded.
    A probe that never returns flips the job to ``failed`` /
    ``TimeoutError``. Note the probe *hangs* rather than raises, so
    ``_dispatchability_failure_reason``'s fail-open ``except Exception``
    does not swallow the cancellation (``CancelledError`` is a
    ``BaseException``, outside ``Exception``).
    """
    registry = IngestJobRegistry()
    job = await _create_running_job(registry)
    result = _pipeline_result(inserted_count=5)

    async def _hanging_probe(_result: IngestionPipelineResult) -> bool:
        await asyncio.Event().wait()  # blocks forever until cancelled
        raise AssertionError("unreachable")  # pragma: no cover

    started = time.monotonic()
    await run_ingest_job(
        job.job_id,
        pipeline_call=lambda: _async_return(result),
        registry=registry,
        dispatchability_check=_hanging_probe,
        timeout_s=0.05,
    )
    elapsed = time.monotonic() - started

    stored = await registry.get(job.job_id, tenant_id=None, is_tenant_admin=True)
    assert stored.status == "failed"
    assert stored.error_class == "TimeoutError"
    # The pipeline returned, but the job is failed (not succeeded/degraded)
    # because the probe never produced a dispatchability verdict.
    assert stored.result is None
    assert elapsed < 5.0


# --- Watchdog budget env loader (#2318) -----------------------------------
#
# ``_load_ingest_job_timeout_seconds`` parses ``INGEST_JOB_TIMEOUT_SECONDS``
# into the finite, positive budget ``asyncio.timeout`` receives. A
# non-finite (``inf`` / ``nan``), non-positive, or malformed value must fall
# back to the 1800 s default with a warning — a misconfigured budget can
# never disable the #2275 watchdog by handing ``asyncio.timeout`` a deadline
# it never schedules.


def test_load_timeout_env_unset_returns_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unset env var yields the 1800 s default with no warning."""
    monkeypatch.delenv("INGEST_JOB_TIMEOUT_SECONDS", raising=False)
    with capture_logs() as logs:
        budget = jobs._load_ingest_job_timeout_seconds()
    assert budget == jobs._DEFAULT_INGEST_JOB_TIMEOUT_SECONDS
    assert not [e for e in logs if e.get("log_level") == "warning"]


def test_load_timeout_env_finite_positive_honored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finite positive override is honored verbatim, with no warning."""
    monkeypatch.setenv("INGEST_JOB_TIMEOUT_SECONDS", "3600")
    with capture_logs() as logs:
        budget = jobs._load_ingest_job_timeout_seconds()
    assert budget == 3600.0
    assert not [e for e in logs if e.get("log_level") == "warning"]


@pytest.mark.parametrize(
    ("raw", "event"),
    [
        ("inf", "ingest_job_timeout_env_out_of_range"),
        ("Infinity", "ingest_job_timeout_env_out_of_range"),
        ("-inf", "ingest_job_timeout_env_out_of_range"),
        ("nan", "ingest_job_timeout_env_out_of_range"),
        ("0", "ingest_job_timeout_env_out_of_range"),
        ("0.0", "ingest_job_timeout_env_out_of_range"),
        ("-30", "ingest_job_timeout_env_out_of_range"),
        ("abc", "ingest_job_timeout_env_invalid"),
        ("", "ingest_job_timeout_env_invalid"),
    ],
)
def test_load_timeout_env_rejected_values_fall_back(
    raw: str,
    event: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-finite, non-positive, and malformed values fall back with a warning.

    ``inf`` / ``-inf`` / ``nan`` parse as floats (no ``ValueError``) but are
    rejected by the ``math.isfinite`` guard; ``0`` / ``0.0`` / negatives by
    the ``<= 0`` guard — both route to ``ingest_job_timeout_env_out_of_range``.
    Empty and non-numeric strings raise ``ValueError`` →
    ``ingest_job_timeout_env_invalid``. Every case returns the finite 1800 s
    default so the watchdog stays armed.
    """
    monkeypatch.setenv("INGEST_JOB_TIMEOUT_SECONDS", raw)
    with capture_logs() as logs:
        budget = jobs._load_ingest_job_timeout_seconds()
    assert budget == jobs._DEFAULT_INGEST_JOB_TIMEOUT_SECONDS
    assert math.isfinite(budget)
    warnings = [e for e in logs if e.get("log_level") == "warning"]
    assert [w["event"] for w in warnings] == [event]


@pytest.mark.asyncio
async def test_inf_env_sanitized_so_watchdog_still_fires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``INGEST_JOB_TIMEOUT_SECONDS=inf`` sanitizes to a finite budget, so a wedged job still fails.

    The correctness bug #2318 closes: ``inf`` slips past a bare ``value <= 0``
    guard, and ``asyncio.timeout(inf)`` schedules no deadline — a wedged job
    would sit at ``running`` forever, re-opening the exact hole the #2275
    watchdog closed. With the ``math.isfinite`` guard ``inf`` falls back to
    the finite default, which is what ``asyncio.timeout`` receives, so the job
    flips to ``failed`` / ``TimeoutError``. The default is shrunk here so the
    finite fallback is observable in-test rather than 30 min later.
    """
    monkeypatch.setenv("INGEST_JOB_TIMEOUT_SECONDS", "inf")
    monkeypatch.setattr(jobs, "_DEFAULT_INGEST_JOB_TIMEOUT_SECONDS", 0.05)
    # Re-resolve the module budget from the (inf) env through the guard,
    # exactly as import time does — the sanitized value is finite, not inf.
    budget = jobs._load_ingest_job_timeout_seconds()
    assert math.isfinite(budget)
    monkeypatch.setattr(jobs, "_INGEST_JOB_TIMEOUT_SECONDS", budget)

    registry = IngestJobRegistry()
    job = await _create_running_job(registry)

    async def _never_returns() -> IngestionPipelineResult:
        await asyncio.Event().wait()  # blocks forever until cancelled
        raise AssertionError("unreachable")  # pragma: no cover

    started = time.monotonic()
    await run_ingest_job(
        job.job_id,
        pipeline_call=_never_returns,
        registry=registry,
        # timeout_s=None → run_ingest_job reads the sanitized module budget,
        # the production path (not the per-call test override).
    )
    elapsed = time.monotonic() - started

    stored = await registry.get(job.job_id, tenant_id=None, is_tenant_admin=True)
    assert stored.status == "failed"
    assert stored.error_class == "TimeoutError"
    assert stored.ended_at is not None
    # Fired on the finite fallback, nowhere near `inf`.
    assert elapsed < 5.0


@pytest.mark.asyncio
async def test_pipeline_log_event_carries_ingest_job_id() -> None:
    """A pipeline log event carries ``ingest_job_id`` via the contextvar binding.

    ``run_ingest_job`` binds the job id into structlog contextvars, so the
    configured ``merge_contextvars`` processor stamps it onto events
    emitted by *other* loggers under the pipeline call -- not just
    jobs.py's own lines. This is what lets a job-id-filtered log grep see
    ``ingestion_pipeline_start`` &c. ``capture_logs`` clears the configured
    processor chain, so ``merge_contextvars`` is re-supplied to mirror the
    production chain (``logging.py``).
    """
    registry = IngestJobRegistry()
    job = await _create_running_job(registry)
    result = _pipeline_result(inserted_count=5)

    async def _pipeline_that_logs() -> IngestionPipelineResult:
        # Mirror the real pipeline: emit from a freshly-bound logger that
        # carries ``connector_id`` but has never seen the job id.
        structlog.get_logger("test.pipeline").bind(connector_id="vrli-rest-9.0").info(
            "ingestion_pipeline_start",
        )
        return result

    async def _check(_result: IngestionPipelineResult) -> bool:
        return True

    with capture_logs(processors=[structlog.contextvars.merge_contextvars]) as logs:
        await run_ingest_job(
            job.job_id,
            pipeline_call=_pipeline_that_logs,
            registry=registry,
            dispatchability_check=_check,
        )

    starts = [entry for entry in logs if entry.get("event") == "ingestion_pipeline_start"]
    assert starts, "pipeline start event was not captured"
    assert starts[0]["ingest_job_id"] == str(job.job_id)
    # The binding must not leak past the run: the contextvar is unbound.
    assert "ingest_job_id" not in structlog.contextvars.get_contextvars()


@pytest.mark.asyncio
async def test_op_id_collision_job_error_names_remediation() -> None:
    """#2273 — the async-job ``error`` field carries the collision remediation.

    The background path loses the structured HTTP ``detail`` shape (no route
    context to raise into) and records only ``str(exc)``. Folding the
    remediation into the exception message is therefore what makes the
    async job's polling response name the fix -- re-ingest under the
    original spec URI, or ``meho.connector.delete`` to clear crashed-job
    debris -- not just the fault.
    """
    registry = IngestJobRegistry()
    job = await _create_running_job(registry)

    async def _raise() -> IngestionPipelineResult:
        raise OpIdCollision(
            op_ids=["GET:/api/items"],
            product="test",
            version="1.0",
            impl_id="test-impl",
            existing_spec_source="https://specs.example.test/a.yaml",
            incoming_spec_source="file:///tmp/a.yaml",
        )

    await run_ingest_job(job.job_id, pipeline_call=_raise, registry=registry)

    stored = await registry.get(job.job_id, tenant_id=None, is_tenant_admin=True)
    assert stored.status == "failed"
    assert stored.error_class == "OpIdCollision"
    assert stored.error is not None
    assert "original spec URI" in stored.error
    assert "meho.connector.delete" in stored.error


@pytest.mark.asyncio
async def test_no_probe_injected_treats_nonempty_insert_as_dispatchable() -> None:
    """A legacy caller that omits the probe still succeeds on a non-empty insert.

    ``dispatchability_check=None`` is the opt-out: the zero-insert guard
    still fires, but the persisted-but-invisible case can only be
    detected by the probe, so an opted-out caller treats any non-empty
    insert as dispatchable (and the production route always supplies the
    probe, so this gap never reaches operators).
    """
    registry = IngestJobRegistry()
    job = await _create_running_job(registry)
    result = _pipeline_result(inserted_count=3)

    await run_ingest_job(
        job.job_id,
        pipeline_call=lambda: _async_return(result),
        registry=registry,
        # No dispatchability_check passed.
    )

    stored = await registry.get(job.job_id, tenant_id=None, is_tenant_admin=True)
    assert stored.status == "succeeded"


@pytest.mark.asyncio
async def test_probe_failure_fails_open_to_succeeded() -> None:
    """A dispatchability probe that *raises* fails open — the job still ``succeeded``.

    The pipeline completed; a transient probe error (e.g. a DB blip on
    the ``connector_exists`` read) must not strand the job in ``running``
    or degrade it on a false signal. The honesty gate only degrades on a
    clean ``False`` verdict or a zero-insert run, not on a probe
    exception.
    """
    registry = IngestJobRegistry()
    job = await _create_running_job(registry)
    result = _pipeline_result(inserted_count=4)

    async def _check_raises(_result: IngestionPipelineResult) -> bool:
        raise RuntimeError("connector_exists probe blew up")

    await run_ingest_job(
        job.job_id,
        pipeline_call=lambda: _async_return(result),
        registry=registry,
        dispatchability_check=_check_raises,
    )

    stored = await registry.get(job.job_id, tenant_id=None, is_tenant_admin=True)
    assert stored.status == "succeeded"
    assert stored.error_class is None
    # Fail-open must not copy the probe exception text into the row either.
    assert stored.error is None


async def _async_return(value: IngestionPipelineResult) -> IngestionPipelineResult:
    """Tiny awaitable that returns *value* — a stand-in pipeline coroutine."""
    return value
