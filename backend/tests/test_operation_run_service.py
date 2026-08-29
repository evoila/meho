# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the async governed dispatch runner (#3079).

Covers :class:`meho_backplane.operations.operation_run_service.OperationRunService`
-- the submit / background-execute / persist / poll / cancel behaviour that
turns a governed dispatch into a durable, pollable run.

The background dispatch (``call_operation`` / the approval resume) is
monkeypatched so these tests are deterministic and never touch a real
connector: the value under test is the *run* machinery (does the envelope
get persisted? does a crash become a ``failed`` run? does a cancel win the
race?), not the dispatch itself. Each test uses a fresh
:class:`OperationRunService` (not the process singleton) so the in-process
task store never leaks across tests, and grabs the launched task from the
store to ``await`` it to completion -- the task is created but has not run
when ``submit_*`` returns, so the grab is race-free.

Runs against the ``sqlite+aiosqlite`` engine the autouse
``_default_database_url`` fixture pre-migrates to head.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import meho_backplane.operations.approval_queue as approval_queue
import meho_backplane.operations.meta_tools as meta_tools
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    OperationRun,
    OperationRunOrigin,
    OperationRunStatus,
    Tenant,
)
from meho_backplane.operations import operation_run as run_lifecycle
from meho_backplane.operations.operation_run import OperationRunNotFoundError
from meho_backplane.operations.operation_run_service import OperationRunService
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _seed_tenant(session: AsyncSession, *, slug: str = "rdc-internal") -> uuid.UUID:
    existing: uuid.UUID | None = await session.scalar(
        select(Tenant.id).where(Tenant.slug == slug),
    )
    if existing is not None:
        return existing
    tenant_id = uuid.uuid4()
    session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
    await session.commit()
    return tenant_id


def _operator(*, tenant_id: uuid.UUID, role: TenantRole = TenantRole.OPERATOR) -> Operator:
    return Operator(sub="op-1", raw_jwt="fake.jwt", tenant_id=tenant_id, tenant_role=role)


_ARGS = {
    "connector_id": "vmware-rest-9.0",
    "op_id": "vm.list",
    "target": "dc-vcenter",
    "params": {"folder": "prod"},
}


async def _drain(service: OperationRunService, run_id: uuid.UUID) -> None:
    """Await the launched background task to completion.

    The task is grabbed from the store synchronously (it is created but has
    not run when ``submit_*`` returns) and awaited fully *before* any other
    DB access, so the test never interleaves DB IO with the background task
    on the single-connection SQLite test engine.
    """
    await service._store[run_id].task


@pytest.mark.asyncio
async def test_submit_call_persists_result_envelope_retrievable_via_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Async submit -> durable handle -> the completed envelope is persisted.

    This is the dropped-response-class fix: after the background dispatch
    completes, the full ``OperationResult`` envelope is retrievable via the
    handle even though nothing was returned to the submitter.
    """

    async def fake_call(operator: Operator, arguments: dict[str, object]) -> dict[str, object]:
        return {
            "status": "ok",
            "op_id": arguments["op_id"],
            "result": {"vms": ["a", "b", "c"]},
            "duration_ms": 42.0,
        }

    monkeypatch.setattr(meta_tools, "call_operation", fake_call)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
    operator = _operator(tenant_id=tenant_id)
    service = OperationRunService()

    # The submit returns a durable handle immediately (a UUID), without
    # blocking for the dispatch.
    run_id = await service.submit_call(operator, dict(_ARGS))
    assert isinstance(run_id, uuid.UUID)
    task = service._store[run_id].task

    await task  # drive the background dispatch to completion

    post = await service.poll(operator, run_id)
    assert post.status == OperationRunStatus.SUCCEEDED.value
    assert post.origin == OperationRunOrigin.DIRECT.value
    assert post.connector_id == "vmware-rest-9.0"
    assert post.target_name == "dc-vcenter"
    assert post.params_hash is not None  # secret-safe correlation, not raw params
    assert post.result == {
        "status": "ok",
        "op_id": "vm.list",
        "result": {"vms": ["a", "b", "c"]},
        "duration_ms": 42.0,
    }
    assert post.ended_at is not None


@pytest.mark.asyncio
async def test_submit_call_records_error_envelope_as_succeeded_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dispatch that returns an *error envelope* is a ``succeeded`` run.

    ``succeeded`` is the *run* completing; the persisted envelope carries the
    dispatch's own ``status`` (here ``error``). The run is not ``failed`` --
    that state is reserved for a run that never produced an envelope.
    """

    async def fake_call(operator: Operator, arguments: dict[str, object]) -> dict[str, object]:
        return {
            "status": "error",
            "op_id": arguments["op_id"],
            "error": "connector 500",
            "duration_ms": 3.0,
            "extras": {"error_code": "connector_http_error"},
        }

    monkeypatch.setattr(meta_tools, "call_operation", fake_call)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
    operator = _operator(tenant_id=tenant_id)
    service = OperationRunService()

    run_id = await service.submit_call(operator, dict(_ARGS))
    await _drain(service, run_id)

    row = await service.poll(operator, run_id)
    assert row.status == OperationRunStatus.SUCCEEDED.value
    assert row.result is not None
    assert row.result["status"] == "error"
    assert row.error is None  # run-level error stays distinct from the envelope


@pytest.mark.asyncio
async def test_submit_call_unexpected_exception_marks_run_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dispatch that *raises* (defence-in-depth) becomes a ``failed`` run."""

    async def boom(operator: Operator, arguments: dict[str, object]) -> dict[str, object]:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(meta_tools, "call_operation", boom)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
    operator = _operator(tenant_id=tenant_id)
    service = OperationRunService()

    run_id = await service.submit_call(operator, dict(_ARGS))
    await _drain(service, run_id)

    row = await service.poll(operator, run_id)
    assert row.status == OperationRunStatus.FAILED.value
    assert row.error is not None and "RuntimeError" in row.error
    assert row.result is None


async def _insert_run(
    tenant_id: uuid.UUID,
    *,
    status: OperationRunStatus = OperationRunStatus.PENDING,
) -> uuid.UUID:
    """Insert a run row directly (no background task) for read/cancel tests."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        run = OperationRun(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            identity_sub="op-1",
            origin=OperationRunOrigin.DIRECT.value,
            connector_id="vmware-rest-9.0",
            op_id="vm.list",
            status=status.value,
        )
        session.add(run)
        await session.commit()
        return run.id


@pytest.mark.asyncio
async def test_cancel_pending_run_via_service_records_intent() -> None:
    """``cancel`` records the durable cancel intent (best-effort cancel)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
    operator = _operator(tenant_id=tenant_id)
    run_id = await _insert_run(tenant_id)
    service = OperationRunService()

    cancelled = await service.cancel(operator, run_id)
    assert cancelled.status == OperationRunStatus.CANCELLED.value
    polled = await service.poll(operator, run_id)
    assert polled.status == OperationRunStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_mark_running_returns_false_for_cancelled_run() -> None:
    """The background task skips the dispatch when the run raced to terminal.

    A cancel that landed before the task started leaves the row terminal;
    ``_mark_running`` returns ``False`` so the op is never sent for a run the
    operator already cancelled.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        run_id = await _seed_and_cancel(session, tenant_id)
    service = OperationRunService()
    assert await service._mark_running(run_id) is False


@pytest.mark.asyncio
async def test_finalize_success_skips_when_run_cancelled() -> None:
    """A cancel that landed mid-dispatch wins: the envelope is discarded.

    When the dispatch completes but the row is already ``cancelled``, the
    finalize transition is illegal and is skipped -- the run stays
    ``cancelled`` and ``result`` stays ``None`` (the dispatch's own
    synchronous audit row is the durable record of what executed).
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        run = OperationRun(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            identity_sub="op-1",
            origin=OperationRunOrigin.DIRECT.value,
            connector_id="vmware-rest-9.0",
            op_id="vm.list",
            status=OperationRunStatus.RUNNING.value,
            started_at=None,
        )
        session.add(run)
        await run_lifecycle.transition(session, run, OperationRunStatus.CANCELLED)
        await session.commit()
        run_id = run.id

    service = OperationRunService()
    await service._finalize_success(run_id, result={"status": "ok", "op_id": "vm.list"})

    operator = _operator(tenant_id=tenant_id)
    row = await service.poll(operator, run_id)
    assert row.status == OperationRunStatus.CANCELLED.value
    assert row.result is None


async def _seed_and_cancel(session: AsyncSession, tenant_id: uuid.UUID) -> uuid.UUID:
    run = OperationRun(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        identity_sub="op-1",
        origin=OperationRunOrigin.DIRECT.value,
        connector_id="vmware-rest-9.0",
        op_id="vm.list",
        status=OperationRunStatus.PENDING.value,
    )
    session.add(run)
    await run_lifecycle.transition(session, run, OperationRunStatus.CANCELLED)
    await session.commit()
    return run.id


@pytest.mark.asyncio
async def test_list_is_tenant_isolated() -> None:
    """``list`` returns only the operator's tenant's runs."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_a = await _seed_tenant(session, slug="rdc-internal")
        tenant_b = await _seed_tenant(session, slug="other-tenant")
    a_run = await _insert_run(tenant_a)
    await _insert_run(tenant_b)
    service = OperationRunService()

    rows = await service.list(_operator(tenant_id=tenant_a))
    assert [r.id for r in rows] == [a_run]


@pytest.mark.asyncio
async def test_poll_cross_tenant_is_not_found() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_a = await _seed_tenant(session, slug="rdc-internal")
        tenant_b = await _seed_tenant(session, slug="other-tenant")
    run_id = await _insert_run(tenant_a)
    service = OperationRunService()

    with pytest.raises(OperationRunNotFoundError):
        await service.poll(_operator(tenant_id=tenant_b), run_id)


@pytest.mark.asyncio
async def test_submit_approval_resume_persists_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An async approval resume runs on the substrate and persists its envelope."""
    request = SimpleNamespace(
        id=uuid.uuid4(),
        params={"disk_gb": 40},
        connector_id="vmware-rest-9.0",
        op_id="vm.create",
    )

    async def fake_resume(*, operator: Operator, request: object, params: object) -> object:
        return SimpleNamespace(
            model_dump=lambda mode="json": {
                "status": "ok",
                "op_id": "vm.create",
                "result": {"vm_id": "vm-99"},
                "duration_ms": 7.0,
            }
        )

    monkeypatch.setattr(approval_queue, "resume_dispatch_after_approval", fake_resume)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
    operator = _operator(tenant_id=tenant_id)
    service = OperationRunService()

    run_id = await service.submit_approval_resume(operator, request, params=None)  # type: ignore[arg-type]
    await _drain(service, run_id)

    row = await service.poll(operator, run_id)
    assert row.status == OperationRunStatus.SUCCEEDED.value
    assert row.origin == OperationRunOrigin.APPROVAL_RESUME.value
    assert row.approval_request_id == request.id
    assert row.result == {
        "status": "ok",
        "op_id": "vm.create",
        "result": {"vm_id": "vm-99"},
        "duration_ms": 7.0,
    }
