# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the per-tick evidence retention sweeper (#2756).

Initiative #2780 (parent goal #221), Task #2756. Coverage:

* A tick with a positive retention window deletes rows older than the cutoff
  and keeps in-window rows.
* ``CHECKS_EVIDENCE_RETENTION_DAYS=0`` is a no-op tick (heartbeat) -- it prunes
  nothing (the chosen 0-semantics: the feature is off, so nothing is written to
  prune, and an already-present row is left untouched) and writes no audit row.
* A non-no-op tick writes exactly one INTERNAL audit row attributed to the
  system sentinel.

Runs on the SQLite engine from :mod:`tests.conftest`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from meho_backplane.checks.evidence_retention import (
    EVIDENCE_RETENTION_PRUNE_PATH,
    SYSTEM_OPERATOR_SUB,
    _run_one_prune_tick,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AuditLog,
    Sensor,
    SensorCadenceKind,
    SensorResult,
    SensorSeverity,
    SensorStatus,
    Tenant,
)
from meho_backplane.settings import get_settings

_ASSERTION = {
    "select": {"path": "$.count"},
    "compare": {"type": "threshold", "op": "lt", "critical": 10},
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _seed_sensor() -> uuid.UUID:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = uuid.uuid4()
        session.add(Tenant(id=tenant_id, slug="ev-ret-tenant", name="Evidence Retention"))
        sensor_id = uuid.uuid4()
        session.add(
            Sensor(
                id=sensor_id,
                tenant_id=tenant_id,
                name="disk-space",
                connector_id="vmware-rest-9.0",
                op_id="vmware.vm.list",
                params={},
                assertion=_ASSERTION,
                status=SensorStatus.ACTIVE.value,
                cadence_kind=SensorCadenceKind.INTERVAL.value,
                interval_seconds=60,
                severity=SensorSeverity.CRITICAL.value,
                for_seconds=0,
                last_state="unknown",
                created_by_sub="op-admin",
            )
        )
        await session.commit()
        return sensor_id


async def _seed_result(sensor_id: uuid.UUID, *, evaluated_at: datetime) -> uuid.UUID:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row_id = uuid.uuid4()
        session.add(
            SensorResult(
                id=row_id,
                sensor_id=sensor_id,
                evaluated_at=evaluated_at,
                state="ok",
                value=1,
                evidence={"observed": 1},
                reason=None,
            )
        )
        await session.commit()
        return row_id


async def _list_result_ids(sensor_id: uuid.UUID) -> set[uuid.UUID]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = await session.execute(
            select(SensorResult.id).where(SensorResult.sensor_id == sensor_id)
        )
        return {r for (r,) in rows.all()}


async def _list_prune_audit_rows() -> list[AuditLog]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = await session.execute(
            select(AuditLog).where(AuditLog.path == EVIDENCE_RETENTION_PRUNE_PATH)
        )
        return list(rows.scalars().all())


@pytest.mark.asyncio
async def test_tick_deletes_past_cutoff_rows_and_keeps_newer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past-cutoff evidence rows are deleted; in-window rows survive."""
    monkeypatch.setenv("CHECKS_EVIDENCE_RETENTION_DAYS", "7")
    get_settings.cache_clear()

    sensor_id = await _seed_sensor()
    old = await _seed_result(sensor_id, evaluated_at=datetime.now(UTC) - timedelta(days=8))
    recent = await _seed_result(sensor_id, evaluated_at=datetime.now(UTC) - timedelta(days=1))

    await _run_one_prune_tick()

    ids = await _list_result_ids(sensor_id)
    assert old not in ids, "past-cutoff evidence row was not deleted"
    assert recent in ids, "in-window evidence row was wrongly deleted"


@pytest.mark.asyncio
async def test_tick_with_retention_zero_prunes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``RETENTION_DAYS=0`` is a no-op tick: it prunes nothing, writes no audit row."""
    monkeypatch.setenv("CHECKS_EVIDENCE_RETENTION_DAYS", "0")
    get_settings.cache_clear()

    sensor_id = await _seed_sensor()
    very_old = await _seed_result(
        sensor_id, evaluated_at=datetime.now(UTC) - timedelta(days=365 * 5)
    )

    await _run_one_prune_tick()

    assert very_old in await _list_result_ids(sensor_id), (
        "RETENTION_DAYS=0 must not prune (feature-off heartbeat)"
    )
    assert await _list_prune_audit_rows() == [], "no-op tick must not write an audit row"


@pytest.mark.asyncio
async def test_tick_writes_one_audit_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tick that runs a real sweep writes exactly one INTERNAL audit row."""
    monkeypatch.setenv("CHECKS_EVIDENCE_RETENTION_DAYS", "7")
    get_settings.cache_clear()

    sensor_id = await _seed_sensor()
    await _seed_result(sensor_id, evaluated_at=datetime.now(UTC) - timedelta(days=8))

    await _run_one_prune_tick()

    audit_rows = await _list_prune_audit_rows()
    assert len(audit_rows) == 1
    assert audit_rows[0].operator_sub == SYSTEM_OPERATOR_SUB
