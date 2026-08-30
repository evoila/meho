# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the flight-recorder retention reaper (#3212, F4).

Covers :mod:`meho_backplane.flight_recorder.reaper` -- the bounded
``expires_at < now()`` sweep that deletes expired traces:

* an expired trace's header + spans are deleted; a fresh trace is kept;
* the referenced ``audit_log`` rows are **never touched** (only a new
  ``INTERNAL`` summary row is added on a non-empty sweep);
* the ``max_per_tick`` limit bounds one tick's deletions;
* an empty sweep writes no summary audit row.

Traces are fixtured directly with a back-dated / forward-dated ``expires_at``
(the operation-run-reaper test's back-date idiom) so the window math is under
the test's control. SQLite FK enforcement is off by default here, which is
exactly why the reaper deletes spans explicitly before headers — this suite
proves that path works without the pragma.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, DispatchTrace, DispatchTraceSpan, Tenant
from meho_backplane.flight_recorder.reaper import (
    FLIGHT_RECORDER_RETENTION_PATH,
    FLIGHT_RECORDER_SYSTEM_TENANT_ID,
    SYSTEM_OPERATOR_SUB,
    _run_one_reap_tick,
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


async def _seed_tenant(session: AsyncSession, *, slug: str) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
    await session.commit()
    return tenant_id


async def _seed_dispatch_audit_row(session: AsyncSession, *, tenant_id: uuid.UUID) -> uuid.UUID:
    """Insert the ``audit_log`` row a trace references (the record of account)."""
    audit_id = uuid.uuid4()
    session.add(
        AuditLog(
            id=audit_id,
            occurred_at=datetime.now(UTC),
            operator_sub="op-1",
            tenant_id=tenant_id,
            method="POST",
            path="/api/v1/operations/call",
            status_code=200,
            duration_ms=Decimal("5.00"),
            payload={},
        )
    )
    await session.commit()
    return audit_id


async def _seed_trace(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    audit_id: uuid.UUID,
    expires_in: timedelta,
    span_count: int = 2,
) -> uuid.UUID:
    """Insert a trace header + *span_count* spans with a controlled expiry."""
    now = datetime.now(UTC)
    trace_id = uuid.uuid4()
    session.add(
        DispatchTrace(
            id=trace_id,
            audit_id=audit_id,
            tenant_id=tenant_id,
            created_at=now - timedelta(days=1),
            expires_at=now + expires_in,
        )
    )
    for seq in range(span_count):
        session.add(
            DispatchTraceSpan(
                id=uuid.uuid4(),
                trace_id=trace_id,
                seq=seq,
                span_kind="vendor_call",
                name=f"span-{seq}",
                started_at=now - timedelta(days=1),
            )
        )
    await session.commit()
    return trace_id


async def _count(session: AsyncSession, model: type) -> int:
    return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


async def test_reaper_deletes_expired_trace_and_keeps_fresh() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session, slug="fr-reap-basic")
        expired_audit = await _seed_dispatch_audit_row(session, tenant_id=tenant_id)
        fresh_audit = await _seed_dispatch_audit_row(session, tenant_id=tenant_id)
        expired_trace = await _seed_trace(
            session, tenant_id=tenant_id, audit_id=expired_audit, expires_in=timedelta(days=-1)
        )
        fresh_trace = await _seed_trace(
            session, tenant_id=tenant_id, audit_id=fresh_audit, expires_in=timedelta(days=+1)
        )

    await _run_one_reap_tick()

    async with sessionmaker() as session:
        header_ids = set((await session.execute(select(DispatchTrace.id))).scalars().all())
        span_trace_ids = set(
            (await session.execute(select(DispatchTraceSpan.trace_id))).scalars().all()
        )
    assert expired_trace not in header_ids
    assert fresh_trace in header_ids
    # Expired trace's spans are gone (explicit span-first delete, no FK pragma).
    assert expired_trace not in span_trace_ids
    assert fresh_trace in span_trace_ids


async def test_reaper_never_touches_audit_log_rows() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session, slug="fr-reap-audit")
        expired_audit = await _seed_dispatch_audit_row(session, tenant_id=tenant_id)
        await _seed_trace(
            session, tenant_id=tenant_id, audit_id=expired_audit, expires_in=timedelta(days=-1)
        )
        dispatch_rows_before = await _count(session, AuditLog)

    await _run_one_reap_tick()

    async with sessionmaker() as session:
        # The referenced dispatch audit row still exists (never deleted).
        still_there = (
            await session.execute(select(AuditLog).where(AuditLog.id == expired_audit))
        ).scalar_one_or_none()
        assert still_there is not None
        # The only *new* audit row is the reaper's own INTERNAL summary row.
        assert await _count(session, AuditLog) == dispatch_rows_before + 1
        summary = (
            await session.execute(
                select(AuditLog).where(AuditLog.path == FLIGHT_RECORDER_RETENTION_PATH)
            )
        ).scalar_one()
    assert summary.operator_sub == SYSTEM_OPERATOR_SUB
    assert summary.tenant_id == FLIGHT_RECORDER_SYSTEM_TENANT_ID
    assert summary.method == "INTERNAL"
    assert summary.payload["dropped_trace_headers"] == 1
    assert summary.payload["dropped_trace_spans"] == 2
    assert "cutoff" in summary.payload


async def test_reaper_respects_max_per_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLIGHT_RECORDER_REAPER_MAX_PER_TICK", "2")
    get_settings.cache_clear()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session, slug="fr-reap-limit")
        for _ in range(3):
            audit_id = await _seed_dispatch_audit_row(session, tenant_id=tenant_id)
            await _seed_trace(
                session, tenant_id=tenant_id, audit_id=audit_id, expires_in=timedelta(days=-1)
            )

    await _run_one_reap_tick()
    async with sessionmaker() as session:
        assert await _count(session, DispatchTrace) == 1  # 3 expired, 2 reaped this tick

    await _run_one_reap_tick()
    async with sessionmaker() as session:
        assert await _count(session, DispatchTrace) == 0  # remaining one reaped next tick


async def test_reaper_empty_sweep_writes_no_summary_row() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session, slug="fr-reap-empty")
        fresh_audit = await _seed_dispatch_audit_row(session, tenant_id=tenant_id)
        await _seed_trace(
            session, tenant_id=tenant_id, audit_id=fresh_audit, expires_in=timedelta(days=+1)
        )

    await _run_one_reap_tick()

    async with sessionmaker() as session:
        summary_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(AuditLog)
                    .where(AuditLog.path == FLIGHT_RECORDER_RETENTION_PATH)
                )
            ).scalar_one()
        )
    assert summary_count == 0
