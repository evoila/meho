# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the :class:`SensorResult` ORM model (#2756).

Initiative #2780 (parent goal #221), Task #2756. The ``sensor_results`` table
is the append-only per-tick evidence history the check runner writes alongside
the :class:`Sensor` latest-state projection.

Coverage matrix
---------------

* **Round-trip persists every field** ``(sensor_id, evaluated_at, state, value,
  evidence, reason)``.
* **Closed-enum CHECK rejects an unknown ``state``.**
* **Drift guards.** The ``ck_sensor_results_state`` value set equals #2504's
  :data:`CheckState`; the migration's frozen literal snapshot agrees.
* **``ON DELETE CASCADE``**: deleting a Sensor drops its history rows (the
  cascade-not-tombstone decision #2756 pins), exercised with SQLite
  foreign-key enforcement opted in.

The tests run against ``sqlite+aiosqlite`` via the shared engine the autouse
``_default_database_url`` fixture pre-migrates to head.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import get_args

import pytest
from sqlalchemy import CheckConstraint, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.checks.assertions import CheckState
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import (
    _SENSOR_LAST_STATES,
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
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def _enforce_sqlite_foreign_keys(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Opt this test in to SQLite foreign-key enforcement.

    ``ON DELETE CASCADE`` only fires when SQLite has ``PRAGMA foreign_keys =
    ON``; flipping ``MEHO_SQLITE_FOREIGN_KEYS=1`` and resetting the cached
    engine rebuilds it with the per-connection PRAGMA listener attached, on top
    of the per-test DB the conftest already migrated to head. Mirrors
    :func:`tests.test_agents_delete_cascade._enforce_sqlite_foreign_keys`.
    """
    monkeypatch.setenv("MEHO_SQLITE_FOREIGN_KEYS", "1")
    reset_engine_for_testing()
    yield
    reset_engine_for_testing()


async def _seed_tenant(session: AsyncSession, *, slug: str = "sr-test-tenant") -> uuid.UUID:
    """Insert a tenant row and return its UUID (slug must not be ``default``)."""
    tenant_id = uuid.uuid4()
    session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
    await session.commit()
    return tenant_id


async def _seed_sensor(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    name: str = "disk-space",
) -> uuid.UUID:
    """Insert a minimal interval Sensor and return its UUID."""
    sensor_id = uuid.uuid4()
    session.add(
        Sensor(
            id=sensor_id,
            tenant_id=tenant_id,
            name=name,
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
            created_by_sub="user-admin",
        )
    )
    await session.commit()
    return sensor_id


@pytest.mark.asyncio
async def test_round_trip_persists_every_field() -> None:
    """Insert a fully-populated :class:`SensorResult`; every field round-trips."""
    sessionmaker = get_sessionmaker()
    evaluated_at = datetime.now(UTC)
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        sensor_id = await _seed_sensor(session, tenant_id)
        row_id = uuid.uuid4()
        session.add(
            SensorResult(
                id=row_id,
                sensor_id=sensor_id,
                evaluated_at=evaluated_at,
                state="critical",
                value=42,
                evidence={"reason": "threshold_breach", "observed": 42},
                reason="threshold_breach",
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        row = (
            await session.execute(select(SensorResult).where(SensorResult.id == row_id))
        ).scalar_one()

    assert row.sensor_id == sensor_id
    assert row.state == "critical"
    assert row.value == 42
    assert row.evidence == {"reason": "threshold_breach", "observed": 42}
    assert row.reason == "threshold_breach"


@pytest.mark.asyncio
async def test_nullable_columns_accept_none() -> None:
    """``value`` / ``evidence`` / ``reason`` accept NULL (an unknown outcome)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        sensor_id = await _seed_sensor(session, tenant_id)
        row_id = uuid.uuid4()
        session.add(
            SensorResult(
                id=row_id,
                sensor_id=sensor_id,
                evaluated_at=datetime.now(UTC),
                state="unknown",
                value=None,
                evidence=None,
                reason=None,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        row = (
            await session.execute(select(SensorResult).where(SensorResult.id == row_id))
        ).scalar_one()
    assert row.value is None
    assert row.evidence is None
    assert row.reason is None


@pytest.mark.asyncio
async def test_state_check_rejects_unknown_value() -> None:
    """``ck_sensor_results_state`` rejects a value outside CheckState."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        sensor_id = await _seed_sensor(session, tenant_id)
        session.add(
            SensorResult(
                id=uuid.uuid4(),
                sensor_id=sensor_id,
                evaluated_at=datetime.now(UTC),
                state="bogus",
                value=None,
                evidence=None,
                reason=None,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.usefixtures("_enforce_sqlite_foreign_keys")
@pytest.mark.asyncio
async def test_delete_sensor_cascades_its_results() -> None:
    """Deleting a Sensor drops its ``sensor_results`` rows (ON DELETE CASCADE)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        sensor_id = await _seed_sensor(session, tenant_id)
        base = datetime.now(UTC)
        for i in range(3):
            session.add(
                SensorResult(
                    id=uuid.uuid4(),
                    sensor_id=sensor_id,
                    evaluated_at=base + timedelta(seconds=i),
                    state="ok",
                    value=i,
                    evidence=None,
                    reason=None,
                )
            )
        await session.commit()

    async with sessionmaker() as session:
        rows_before = (
            (await session.execute(select(SensorResult).where(SensorResult.sensor_id == sensor_id)))
            .scalars()
            .all()
        )
        assert len(rows_before) == 3
        sensor = await session.get(Sensor, sensor_id)
        assert sensor is not None
        await session.delete(sensor)
        await session.commit()

    async with sessionmaker() as session:
        rows_after = (
            (await session.execute(select(SensorResult).where(SensorResult.sensor_id == sensor_id)))
            .scalars()
            .all()
        )
    assert rows_after == [], "sensor delete must cascade-delete its evidence rows"


def _orm_check_bodies() -> dict[str, str]:
    """Map each named CHECK on the ``sensor_results`` ORM table to its SQL body."""
    return {
        c.name: str(c.sqltext)
        for c in SensorResult.__table__.constraints
        if isinstance(c, CheckConstraint) and c.name is not None
    }


def test_state_value_set_equals_checkstate() -> None:
    """``ck_sensor_results_state`` covers exactly #2504's ``CheckState`` members."""
    body = _orm_check_bodies()["ck_sensor_results_state"]
    for member in get_args(CheckState):
        assert f"'{member}'" in body
    # And the ORM derives its CHECK from the shared _SENSOR_LAST_STATES tuple.
    assert set(_SENSOR_LAST_STATES) == set(get_args(CheckState))


def _load_migration_by_name(name: str) -> object:
    """Load an Alembic migration module by its file basename."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "alembic" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_migration_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_frozen_state_literal_equals_checkstate() -> None:
    """Migration 0071's frozen ``state`` literal is a snapshot of ``CheckState``."""
    migration = _load_migration_by_name("0071_create_sensor_results")
    assert set(migration._SENSOR_RESULT_STATES) == set(get_args(CheckState))  # type: ignore[attr-defined]
