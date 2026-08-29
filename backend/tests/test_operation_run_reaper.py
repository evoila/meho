# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the operation-run reaper (#3079).

Covers :mod:`meho_backplane.operations.operation_run_reaper` -- the sweep
that reclaims a governed dispatch whose worker died mid-flight. The single
policy is fail-into-audit: an orphaned run is driven to ``failed`` (an
audited terminal state) and **never** re-dispatched (a governed op can wrap
a non-idempotent vendor write). This is the "never silently lost" half of
the #3079 lease/reaper acceptance criterion.

These tests fixture a run directly into the DB (no runner) and back-date its
``lease_expires_at`` to simulate a dead worker, then drive the reaper's tick
directly. On SQLite the advisory lock is a no-op (single-replica test
process). Runs against the ``sqlite+aiosqlite`` engine the autouse
``_default_database_url`` fixture pre-migrates to head.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AuditLog,
    OperationRun,
    OperationRunOrigin,
    OperationRunStatus,
    Tenant,
)
from meho_backplane.operations.operation_run_reaper import (
    OPERATION_RUN_REAPER_INTERRUPTION_REASON,
    _run_one_tick,
)
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


async def _seed_running_run(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    status: OperationRunStatus = OperationRunStatus.RUNNING,
    lease_age_seconds: float | None = 120.0,
) -> uuid.UUID:
    """Insert a run in *status*; back-date the lease when *lease_age_seconds* is set."""
    run = OperationRun(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        identity_sub="op-1",
        origin=OperationRunOrigin.DIRECT.value,
        connector_id="vmware-rest-9.0",
        op_id="vm.create",
        status=status.value,
        lease_owner="dead-pod:42",
        started_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    if lease_age_seconds is not None:
        run.lease_expires_at = datetime.now(UTC) - timedelta(seconds=lease_age_seconds)
    session.add(run)
    await session.commit()
    return run.id


@pytest.mark.asyncio
async def test_expired_lease_running_run_is_failed_and_audited() -> None:
    """An expired-lease ``running`` run is driven to ``failed`` + audited."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        run_id = await _seed_running_run(session, tenant_id=tenant_id)

    await _run_one_tick()

    async with sessionmaker() as session:
        run = await session.get(OperationRun, run_id)
        assert run is not None
        assert run.status == OperationRunStatus.FAILED.value
        assert run.error == OPERATION_RUN_REAPER_INTERRUPTION_REASON
        assert run.ended_at is not None
        assert run.lease_owner is None and run.lease_expires_at is None
        # Result is never populated -- the op is not re-dispatched.
        assert run.result is None

        audit_rows = (
            (await session.execute(select(AuditLog).where(AuditLog.run_id == run_id)))
            .scalars()
            .all()
        )
        assert len(audit_rows) == 1
        assert audit_rows[0].operator_sub == "system:operation-run-reaper"
        assert audit_rows[0].path == "internal/operation-run/reaper/fail-into-audit"


@pytest.mark.asyncio
async def test_reaper_ignores_running_run_with_fresh_lease() -> None:
    """A healthy worker (lease in the future) is not reclaimed."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        run = OperationRun(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            identity_sub="op-1",
            origin=OperationRunOrigin.DIRECT.value,
            connector_id="vmware-rest-9.0",
            op_id="vm.create",
            status=OperationRunStatus.RUNNING.value,
            lease_owner="live-pod:7",
            lease_expires_at=datetime.now(UTC) + timedelta(seconds=120),
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    await _run_one_tick()

    async with sessionmaker() as session:
        row = await session.get(OperationRun, run_id)
        assert row is not None
        assert row.status == OperationRunStatus.RUNNING.value
        audit = (
            (await session.execute(select(AuditLog).where(AuditLog.run_id == run_id)))
            .scalars()
            .all()
        )
        assert audit == []


@pytest.mark.asyncio
async def test_reaper_ignores_pending_run_even_with_stale_lease() -> None:
    """A ``pending`` run is not reaped (the claim query filters on ``running``)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        run_id = await _seed_running_run(
            session, tenant_id=tenant_id, status=OperationRunStatus.PENDING
        )

    await _run_one_tick()

    async with sessionmaker() as session:
        row = await session.get(OperationRun, run_id)
        assert row is not None
        assert row.status == OperationRunStatus.PENDING.value


@pytest.mark.asyncio
async def test_reaper_handles_empty_table_cleanly() -> None:
    """A tick with no expired rows does nothing and writes no audit row."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await _seed_tenant(session)

    await _run_one_tick()

    async with sessionmaker() as session:
        audit = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.operator_sub == "system:operation-run-reaper")
                )
            )
            .scalars()
            .all()
        )
        assert audit == []
