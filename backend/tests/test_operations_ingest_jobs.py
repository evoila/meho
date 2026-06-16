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

import pytest

from meho_backplane.operations.ingest import (
    IngestionPipelineResult,
    IngestJob,
    IngestJobRegistry,
    run_ingest_job,
)
from meho_backplane.operations.ingest.jobs import INGESTED_NOT_DISPATCHABLE
from meho_backplane.operations.ingest.register_ingested import IngestionResult


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
