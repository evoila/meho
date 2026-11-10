# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the step a running ingest job shows (#5786).

A running ingest job says which step it is on (``parse`` -> ``register``
-> ``grouping``) and since when, so an operator can tell a slow import
from a stuck one. These tests drive the real
:meth:`IngestionPipelineService.ingest` inside :func:`run_ingest_job`,
with the parse, register and grouping work replaced by stubs, so no DB,
parser or language model runs.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest

from meho_backplane.api.v1 import connectors_ingest as rest_mod
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.tools import connector_ingest as mcp_mod
from meho_backplane.operations.ingest import (
    IngestionPipelineResult,
    IngestionPipelineService,
    IngestJob,
    IngestJobRegistry,
    SpecSource,
    run_ingest_job,
)
from meho_backplane.operations.ingest.job_phase import report_ingest_phase
from meho_backplane.operations.ingest.jobs import GROUPING_FAILED_AFTER_REGISTER
from meho_backplane.operations.ingest.llm_groups import GroupingResult
from meho_backplane.operations.ingest.register_ingested import IngestionResult

_TENANT_ID = uuid.uuid4()


def _ingestion() -> IngestionResult:
    return IngestionResult(
        inserted_count=4,
        updated_count=0,
        skipped_count=0,
        connector_registered=True,
        operations_grouped=False,
    )


def _grouping() -> GroupingResult:
    return GroupingResult(
        connector_id="vmware-rest-9.0",
        groups_created=2,
        operations_assigned=4,
        operations_unassigned=0,
        llm_call_count=2,
        llm_duration_ms=1.0,
    )


async def _create_job(registry: IngestJobRegistry) -> IngestJob:
    return await registry.create(
        operator_sub="op-phase",
        tenant_id=_TENANT_ID,
        catalog_entry=None,
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        spec_uris=["https://example.invalid/vmware.yaml"],
    )


def _service(
    monkeypatch: pytest.MonkeyPatch,
    job: IngestJob,
    seen: list[tuple[str, str, float]],
    *,
    grouping_error: Exception | None = None,
) -> IngestionPipelineService:
    """A real pipeline service whose three steps only record the job's phase."""
    operator = Operator(
        sub="op-phase",
        raw_jwt="jwt",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
    )
    service = IngestionPipelineService(operator, sessionmaker=MagicMock())

    def _validate(**_kwargs: Any) -> None:
        seen.append(("validate", job.phase, job.phase_started_at))

    async def _register(**_kwargs: Any) -> IngestionResult:
        seen.append(("register", job.phase, job.phase_started_at))
        return _ingestion()

    async def _group(**_kwargs: Any) -> GroupingResult:
        seen.append(("grouping", job.phase, job.phase_started_at))
        if grouping_error is not None:
            raise grouping_error
        return _grouping()

    monkeypatch.setattr(service, "_validate_spec_versions", _validate)
    monkeypatch.setattr(service, "_run_register_phase", _register)
    monkeypatch.setattr(service, "_run_grouping_phase", _group)
    return service


async def _run(
    service: IngestionPipelineService, job: IngestJob, registry: IngestJobRegistry
) -> None:
    async def _pipeline_call() -> IngestionPipelineResult:
        return await service.ingest(
            product="vmware",
            version="9.0",
            impl_id="vmware-rest",
            specs=[SpecSource(uri="https://example.invalid/vmware.yaml", content="{}")],
            tenant_id=_TENANT_ID,
        )

    async def _dispatchable(_result: IngestionPipelineResult) -> bool:
        return True

    await run_ingest_job(
        job.job_id,
        pipeline_call=_pipeline_call,
        registry=registry,
        dispatchability_check=_dispatchable,
    )


async def test_new_job_starts_in_parse_at_its_start_time() -> None:
    registry = IngestJobRegistry()
    job = await _create_job(registry)
    assert job.status == "running"
    assert job.phase == "parse"
    assert job.phase_started_at == job.started_at


async def test_phase_moves_forward_in_order_during_an_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """parse -> register -> grouping, each with a later start time; the end keeps grouping."""
    registry = IngestJobRegistry()
    job = await _create_job(registry)
    seen: list[tuple[str, str, float]] = []
    service = _service(monkeypatch, job, seen)

    await _run(service, job, registry)

    assert [(step, phase) for step, phase, _ in seen] == [
        ("validate", "parse"),
        ("register", "register"),
        ("grouping", "grouping"),
    ]
    times = [started for _, _, started in seen]
    assert times == sorted(times)
    assert times[0] == job.started_at
    assert times[1] >= times[0]

    assert job.status == "succeeded"
    assert job.phase == "grouping"
    assert job.phase_started_at == times[2]
    assert job.error is None
    assert job.error_class is None


async def test_failed_job_keeps_the_phase_it_failed_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = IngestJobRegistry()
    job = await _create_job(registry)
    seen: list[tuple[str, str, float]] = []
    service = _service(monkeypatch, job, seen)

    async def _register_fails(**_kwargs: Any) -> IngestionResult:
        raise RuntimeError("database went away")

    monkeypatch.setattr(service, "_run_register_phase", _register_fails)

    await _run(service, job, registry)

    assert job.status == "failed"
    assert job.phase == "register"
    assert job.error_class == "RuntimeError"
    assert job.result is None


async def test_degraded_job_keeps_grouping_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = IngestJobRegistry()
    job = await _create_job(registry)
    seen: list[tuple[str, str, float]] = []
    service = _service(monkeypatch, job, seen, grouping_error=RuntimeError("model timed out"))

    await _run(service, job, registry)

    assert job.status == "degraded"
    assert job.error_class == GROUPING_FAILED_AFTER_REGISTER
    assert job.error is not None
    assert "RuntimeError: model timed out" in job.error
    assert job.phase == "grouping"


async def test_set_phase_ignores_finished_and_unknown_jobs() -> None:
    registry = IngestJobRegistry()
    job = await _create_job(registry)
    await registry.fail(job.job_id, error=RuntimeError("boom"))
    before = job.phase_started_at

    await registry.set_phase(job.job_id, "grouping")
    await registry.set_phase(uuid.uuid4(), "grouping")

    assert job.phase == "parse"
    assert job.phase_started_at == before


async def test_report_outside_a_job_does_nothing() -> None:
    """The sync ingest path runs no job: a phase report there is a no-op."""
    await report_ingest_phase("register")


@pytest.mark.parametrize("project", [rest_mod._job_to_response, mcp_mod._job_to_response])
async def test_status_answer_carries_the_phase(project: Any) -> None:
    """REST and MCP status answers both show the phase; running leaves the result empty."""
    registry = IngestJobRegistry()
    job = await _create_job(registry)
    await registry.set_phase(job.job_id, "register")

    running = project(job).model_dump(mode="json")
    assert running["status"] == "running"
    assert running["phase"] == "register"
    assert running["phase_started_at"] == job.phase_started_at
    assert running["phase_started_at"] >= running["started_at"]
    assert running["ingestion"] is None
    assert running["error"] is None

    await registry.fail(job.job_id, error=RuntimeError("boom"))
    failed = project(job).model_dump(mode="json")
    assert failed["status"] == "failed"
    assert failed["phase"] == "register"
    assert failed["error_class"] == "RuntimeError"
    assert failed["ingestion"] is None
