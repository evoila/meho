# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the :class:`Sensor` ORM model + closed enums.

Initiative #2416 (parent goal #221), Task #2503. The ``sensor`` table is
the deterministic check layer's registration substrate -- one row per
check pinning an ``(op + args + assertion + cadence + severity)`` tuple.
It is a single-table cadence discriminated union (interval / cron) with a
latest-result projection on the row, modelled on :class:`ScheduledTrigger`.

Coverage matrix
---------------

* **Round-trip persists every field for each cadence kind.**
* **ORM defaults fire on SQLite** (``status`` / ``severity`` /
  ``last_state`` / ``for_seconds`` / ``identity_sub`` / ``timezone`` /
  ``id`` / ``created_at``).
* **Closed-enum CHECKs reject unknown** (``cadence_kind`` / ``status`` /
  ``severity`` / ``last_state``).
* **Cadence discriminated-union CHECK rejects malformed rows.**
* **tenant FK enforced** + **unique (tenant_id, name) enforced.**
* **Drift guards.** The model enums and the migration's frozen literals
  agree; the migration's frozen CHECK bodies equal the ORM's; and the
  ``ck_sensor_last_state`` value set equals #2504's :data:`CheckState`.

The tests run against ``sqlite+aiosqlite`` via the shared engine the
autouse ``_default_database_url`` fixture pre-migrates to head.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import get_args

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.checks.assertions import CheckState
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    _SENSOR_CADENCE_FIELDS_CHECK,
    _SENSOR_CADENCE_KINDS,
    _SENSOR_LAST_STATES,
    _SENSOR_SEVERITIES,
    _SENSOR_STATUSES,
    Sensor,
    SensorCadenceKind,
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


async def _enable_sqlite_foreign_keys(session: AsyncSession) -> None:
    """Issue ``PRAGMA foreign_keys = ON`` on the bound SQLite connection."""
    await session.execute(text("PRAGMA foreign_keys = ON"))


async def _seed_tenant(
    session: AsyncSession,
    *,
    tenant_slug: str = "sensor-test-tenant",
) -> uuid.UUID:
    """Insert a tenant row and return its UUID.

    The slug must not be ``default`` (migration ``0028`` seeds a real
    tenant with that slug into the migrated test DB).
    """
    tenant_id = uuid.uuid4()
    session.add(Tenant(id=tenant_id, slug=tenant_slug, name=f"Tenant {tenant_slug}"))
    await session.commit()
    return tenant_id


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interval_sensor_round_trip_persists_every_field() -> None:
    """Insert a fully-populated interval :class:`Sensor`; all fields round-trip."""
    sessionmaker = get_sessionmaker()
    sensor_id = uuid.uuid4()
    now = datetime.now(UTC)

    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        session.add(
            Sensor(
                id=sensor_id,
                tenant_id=tenant_id,
                name="disk-space",
                connector_id="vmware-rest-9.0",
                op_id="vmware.vm.list",
                target={"target_id": "abc"},
                params={"limit": 5},
                assertion=_ASSERTION,
                status=SensorStatus.ACTIVE.value,
                cadence_kind=SensorCadenceKind.INTERVAL.value,
                interval_seconds=60,
                cron_expr=None,
                next_fire_at=now,
                severity=SensorSeverity.DEGRADED.value,
                for_seconds=300,
                last_state="ok",
                created_by_sub="user-admin",
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        row = (await session.execute(select(Sensor).where(Sensor.id == sensor_id))).scalar_one()

    assert row.tenant_id == tenant_id
    assert row.name == "disk-space"
    assert row.connector_id == "vmware-rest-9.0"
    assert row.op_id == "vmware.vm.list"
    assert row.target == {"target_id": "abc"}
    assert row.params == {"limit": 5}
    assert row.assertion == _ASSERTION
    assert row.cadence_kind == "interval"
    assert row.interval_seconds == 60
    assert row.cron_expr is None
    assert row.severity == "degraded"
    assert row.for_seconds == 300
    assert row.last_state == "ok"
    assert row.status == "active"


@pytest.mark.asyncio
async def test_cron_sensor_round_trip_persists_every_field() -> None:
    """Insert a fully-populated cron :class:`Sensor`; all fields round-trip."""
    sessionmaker = get_sessionmaker()
    sensor_id = uuid.uuid4()

    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        session.add(
            Sensor(
                id=sensor_id,
                tenant_id=tenant_id,
                name="nightly-check",
                connector_id="vmware-rest-9.0",
                op_id="vmware.vm.list",
                assertion=_ASSERTION,
                cadence_kind=SensorCadenceKind.CRON.value,
                cron_expr="0 9 * * *",
                interval_seconds=None,
                timezone="Europe/Sarajevo",
                created_by_sub="user-admin",
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        row = (await session.execute(select(Sensor).where(Sensor.id == sensor_id))).scalar_one()

    assert row.cadence_kind == "cron"
    assert row.cron_expr == "0 9 * * *"
    assert row.interval_seconds is None
    assert row.timezone == "Europe/Sarajevo"


# ---------------------------------------------------------------------------
# ORM defaults
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orm_defaults_fire_on_sqlite() -> None:
    """``status`` / ``severity`` / ``last_state`` / ``for_seconds`` / etc. default Python-side."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        sensor = Sensor(
            tenant_id=tenant_id,
            name="minimal",
            connector_id="vmware-rest-9.0",
            op_id="vmware.vm.list",
            assertion=_ASSERTION,
            cadence_kind=SensorCadenceKind.INTERVAL.value,
            interval_seconds=30,
            created_by_sub="user-min",
        )
        session.add(sensor)
        await session.commit()
        seen_id = sensor.id
        assert isinstance(seen_id, uuid.UUID)
        assert sensor.status == SensorStatus.ACTIVE.value
        assert sensor.severity == SensorSeverity.CRITICAL.value
        assert sensor.last_state == "unknown"
        assert sensor.for_seconds == 0
        assert sensor.identity_sub == "__sensor__"
        assert sensor.timezone == "UTC"
        assert sensor.created_at is not None

    async with sessionmaker() as session:
        row = (await session.execute(select(Sensor).where(Sensor.id == seen_id))).scalar_one()
    assert row.params == {}
    assert row.target is None
    assert row.status_reason is None
    assert row.cron_expr is None
    assert row.next_fire_at is None
    assert row.last_value is None
    assert row.last_evidence is None
    assert row.state_since is None


# ---------------------------------------------------------------------------
# Closed-enum CHECK constraints
# ---------------------------------------------------------------------------


async def _seed_and_add(sensor_kwargs: dict[str, object]) -> None:
    """Seed a tenant then add a Sensor with *sensor_kwargs*, expecting IntegrityError."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        base: dict[str, object] = {
            "tenant_id": tenant_id,
            "name": "bad",
            "connector_id": "vmware-rest-9.0",
            "op_id": "vmware.vm.list",
            "assertion": _ASSERTION,
            "created_by_sub": "user-bad",
        }
        base.update(sensor_kwargs)
        session.add(Sensor(**base))
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_cadence_kind_check_rejects_unknown() -> None:
    await _seed_and_add({"cadence_kind": "hourly", "interval_seconds": 60})


@pytest.mark.asyncio
async def test_status_check_rejects_unknown() -> None:
    await _seed_and_add(
        {
            "cadence_kind": SensorCadenceKind.INTERVAL.value,
            "interval_seconds": 60,
            "status": "archived",
        }
    )


@pytest.mark.asyncio
async def test_severity_check_rejects_unknown() -> None:
    await _seed_and_add(
        {
            "cadence_kind": SensorCadenceKind.INTERVAL.value,
            "interval_seconds": 60,
            "severity": "fatal",
        }
    )


@pytest.mark.asyncio
async def test_last_state_check_rejects_unknown() -> None:
    await _seed_and_add(
        {
            "cadence_kind": SensorCadenceKind.INTERVAL.value,
            "interval_seconds": 60,
            "last_state": "warn",
        }
    )


# ---------------------------------------------------------------------------
# Cadence discriminated-union CHECK
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interval_kind_requires_interval_seconds() -> None:
    """``cadence_kind='interval'`` with ``interval_seconds IS NULL`` fails the discriminator."""
    await _seed_and_add({"cadence_kind": SensorCadenceKind.INTERVAL.value})


@pytest.mark.asyncio
async def test_interval_kind_rejects_cron_expr() -> None:
    """``cadence_kind='interval'`` with ``cron_expr`` populated fails the discriminator."""
    await _seed_and_add(
        {
            "cadence_kind": SensorCadenceKind.INTERVAL.value,
            "interval_seconds": 60,
            "cron_expr": "* * * * *",
        }
    )


@pytest.mark.asyncio
async def test_cron_kind_requires_cron_expr() -> None:
    """``cadence_kind='cron'`` with ``cron_expr IS NULL`` fails the discriminator."""
    await _seed_and_add({"cadence_kind": SensorCadenceKind.CRON.value})


@pytest.mark.asyncio
async def test_cron_kind_rejects_interval_seconds() -> None:
    """``cadence_kind='cron'`` with ``interval_seconds`` populated fails the discriminator."""
    await _seed_and_add(
        {
            "cadence_kind": SensorCadenceKind.CRON.value,
            "cron_expr": "* * * * *",
            "interval_seconds": 60,
        }
    )


# ---------------------------------------------------------------------------
# Foreign-key + uniqueness constraints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_fk_enforced() -> None:
    """A dangling ``tenant_id`` raises :class:`IntegrityError` under FK enforcement."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await _enable_sqlite_foreign_keys(session)
        session.add(
            Sensor(
                tenant_id=uuid.uuid4(),  # no such tenant row
                name="orphan",
                connector_id="vmware-rest-9.0",
                op_id="vmware.vm.list",
                assertion=_ASSERTION,
                cadence_kind=SensorCadenceKind.INTERVAL.value,
                interval_seconds=60,
                created_by_sub="user-orphan",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_unique_name_per_tenant_enforced() -> None:
    """Two sensors with the same (tenant_id, name) collide on the unique index."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)

        def _make() -> Sensor:
            return Sensor(
                tenant_id=tenant_id,
                name="dupe",
                connector_id="vmware-rest-9.0",
                op_id="vmware.vm.list",
                assertion=_ASSERTION,
                cadence_kind=SensorCadenceKind.INTERVAL.value,
                interval_seconds=60,
                created_by_sub="user-admin",
            )

        session.add(_make())
        await session.commit()
        session.add(_make())
        with pytest.raises(IntegrityError):
            await session.commit()


# ---------------------------------------------------------------------------
# Drift guards
# ---------------------------------------------------------------------------


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


def _orm_check_bodies() -> dict[str, str]:
    """Map each named CHECK on the ``sensor`` ORM table to its SQL body string."""
    from sqlalchemy import CheckConstraint

    return {
        c.name: str(c.sqltext)
        for c in Sensor.__table__.constraints
        if isinstance(c, CheckConstraint) and c.name is not None
    }


def test_cadence_kinds_match_enum() -> None:
    assert set(_SENSOR_CADENCE_KINDS) == {k.value for k in SensorCadenceKind}


def test_statuses_match_enum() -> None:
    assert set(_SENSOR_STATUSES) == {s.value for s in SensorStatus}


def test_severities_match_enum() -> None:
    assert set(_SENSOR_SEVERITIES) == {s.value for s in SensorSeverity}


def test_last_state_value_set_equals_checkstate() -> None:
    """``ck_sensor_last_state``'s value set equals #2504's ``CheckState`` members.

    AC (b): the five-state vocabulary is declared once in
    ``checks.assertions.CheckState``; ``db.models`` derives the CHECK
    literals from it and never re-declares the enum.
    """
    assert set(_SENSOR_LAST_STATES) == set(get_args(CheckState))
    # And the CHECK body actually mentions every member.
    last_state_body = _orm_check_bodies()["ck_sensor_last_state"]
    for member in get_args(CheckState):
        assert f"'{member}'" in last_state_body


def test_migration_frozen_tuples_match_model_enums() -> None:
    """The migration's frozen literal tuples match the model enums."""
    migration = _load_migration_by_name("0064_create_sensor")
    assert set(migration._SENSOR_CADENCE_KINDS) == {k.value for k in SensorCadenceKind}  # type: ignore[attr-defined]
    assert set(migration._SENSOR_STATUSES) == {s.value for s in SensorStatus}  # type: ignore[attr-defined]
    assert set(migration._SENSOR_SEVERITIES) == {s.value for s in SensorSeverity}  # type: ignore[attr-defined]


def test_migration_last_state_literal_matches_checkstate() -> None:
    """The migration's frozen ``last_state`` literal is a snapshot of ``CheckState``."""
    migration = _load_migration_by_name("0064_create_sensor")
    assert set(migration._SENSOR_LAST_STATES) == set(get_args(CheckState))  # type: ignore[attr-defined]


def test_migration_check_bodies_equal_orm() -> None:
    """AC (a): the migration's frozen CHECK bodies equal the ORM's.

    Renders each closed-enum CHECK body from the migration's frozen tuples
    and compares to the live ORM constraint's SQL text; also compares the
    cadence discriminated-union body byte-for-byte.
    """
    migration = _load_migration_by_name("0064_create_sensor")
    orm = _orm_check_bodies()

    assert orm["ck_sensor_cadence_kind"] == migration._check_in(  # type: ignore[attr-defined]
        "cadence_kind",
        migration._SENSOR_CADENCE_KINDS,  # type: ignore[attr-defined]
    )
    assert orm["ck_sensor_status"] == migration._check_in(  # type: ignore[attr-defined]
        "status",
        migration._SENSOR_STATUSES,  # type: ignore[attr-defined]
    )
    assert orm["ck_sensor_severity"] == migration._check_in(  # type: ignore[attr-defined]
        "severity",
        migration._SENSOR_SEVERITIES,  # type: ignore[attr-defined]
    )
    assert orm["ck_sensor_last_state"] == migration._check_in(  # type: ignore[attr-defined]
        "last_state",
        migration._SENSOR_LAST_STATES,  # type: ignore[attr-defined]
    )
    # The discriminated-union body is the non-trivial one; assert both the
    # ORM constant and the live constraint match the migration's frozen body.
    assert migration._SENSOR_CADENCE_FIELDS_CHECK == _SENSOR_CADENCE_FIELDS_CHECK  # type: ignore[attr-defined]
    assert orm["ck_sensor_cadence_fields"] == _SENSOR_CADENCE_FIELDS_CHECK
