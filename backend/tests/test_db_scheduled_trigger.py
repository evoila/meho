# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the :class:`ScheduledTrigger` ORM model + closed enums.

Initiative #804 (G11.3 Scheduler), Task #822 (T1). The
``scheduled_trigger`` table stores all three trigger shapes (cron,
one-off, event) the G11.3 scheduler dispatches as a single-table
discriminated union; T1 settles the durability-substrate fork (Option A
-- extend the existing roll-our-own ``asyncio`` + advisory-lock
pattern) and lands the storage shape T2 / T3 / T4 / T5 build on.

Coverage matrix
---------------

* **Round-trip persists every field for each kind.** Insert a fully-
  populated cron / one_off / event :class:`ScheduledTrigger`, read it
  back, every column round-trips.
* **ORM defaults fire on SQLite.** ``id`` / ``status`` /
  ``in_flight_policy`` / ``created_at`` populated Python-side where
  the migration's PG server defaults are no-ops; the nullable columns
  default to ``None``.
* **kind CHECK rejects unknown.** A ``kind`` outside
  :class:`ScheduledTriggerKind` raises :class:`IntegrityError`.
* **status CHECK rejects unknown.** Same for ``status`` /
  :class:`ScheduledTriggerStatus`.
* **in_flight_policy CHECK rejects unknown.** Same for ``in_flight_policy``
  / :class:`ScheduledTriggerInFlightPolicy`.
* **Discriminated-union CHECK rejects malformed rows.** A
  ``kind = 'cron'`` row with ``cron_expr IS NULL``, a
  ``kind = 'one_off'`` row with ``cron_expr`` populated, and an
  ``event`` row with no ``event_filter`` each raise
  :class:`IntegrityError`.
* **tenant FK enforced.** A trigger row with a dangling ``tenant_id``
  raises :class:`IntegrityError` under ``PRAGMA foreign_keys = ON``.
* **agent_definition FK enforced.** A trigger row with a dangling
  ``agent_definition_id`` raises :class:`IntegrityError`.
* **Drift guards.** The model enums and the live ``CHECK`` constraints
  agree; the migration's frozen literal tuples match the model enums.

The tests run synchronously against ``sqlite+aiosqlite`` via the shared
engine cache the autouse ``_default_database_url`` fixture in
:mod:`tests.conftest` pre-migrates to head. PG-real shape parity is
covered by the testcontainers replay suite in
:mod:`tests.test_migration_rollback`. This file stays Docker-free so
the always-on gate asserts the ORM contract on every PR.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    _SCHEDULED_TRIGGER_IN_FLIGHT_POLICIES,
    _SCHEDULED_TRIGGER_KINDS,
    _SCHEDULED_TRIGGER_STATUSES,
    AgentDefinition,
    ScheduledTrigger,
    ScheduledTriggerInFlightPolicy,
    ScheduledTriggerKind,
    ScheduledTriggerStatus,
    Tenant,
)
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module.

    The autouse ``_default_database_url`` fixture only pins
    ``DATABASE_URL``; Keycloak/Vault knobs come from each test file.
    The ``get_settings.cache_clear()`` brackets keep a stale ``Settings``
    instance from a previous test from leaking.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _enable_sqlite_foreign_keys(session: AsyncSession) -> None:
    """Issue ``PRAGMA foreign_keys = ON`` on the bound SQLite connection.

    SQLite ships with FK enforcement disabled by default; without this
    PRAGMA the FK-violation tests would silently no-op. The shared
    engine uses :class:`StaticPool` on SQLite so one connection backs
    every checkout in this test process. Mirrors the helper in
    :mod:`tests.test_db_agent_run`.
    """
    await session.execute(text("PRAGMA foreign_keys = ON"))


async def _seed_tenant_and_definition(
    session: AsyncSession,
    *,
    tenant_slug: str = "scheduler-test-tenant",
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a tenant + agent_definition row, return their UUIDs.

    ``scheduled_trigger`` carries real FKs to both tables, so the
    per-test setup needs both rows to avoid spurious
    :class:`IntegrityError` under PRAGMA foreign_keys=ON.

    The slug must not be ``rdc-internal``: migration 0018 (G7.1-T5 #317)
    seeds a real tenant with that slug into the migrated test DB, so
    reusing it collides on the unique ``tenant.slug`` constraint.
    """
    tenant_id = uuid.uuid4()
    definition_id = uuid.uuid4()
    session.add(Tenant(id=tenant_id, slug=tenant_slug, name=f"Tenant {tenant_slug}"))
    session.add(
        AgentDefinition(
            id=definition_id,
            tenant_id=tenant_id,
            name="incident-triage",
            identity_ref="keycloak:agent:triage",
            model_tier="standard",
            system_prompt="You are an incident triage agent.",
            toolset={},
            turn_budget=10,
            created_by_sub="user-admin",
        )
    )
    await session.commit()
    return tenant_id, definition_id


# ---------------------------------------------------------------------------
# Round-trip -- one assertion per kind
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cron_trigger_round_trip_persists_every_field() -> None:
    """Insert a fully-populated cron :class:`ScheduledTrigger`, all fields round-trip."""
    sessionmaker = get_sessionmaker()
    trigger_id = uuid.uuid4()
    now = datetime.now(UTC)

    async with sessionmaker() as session:
        tenant_id, definition_id = await _seed_tenant_and_definition(session)
        session.add(
            ScheduledTrigger(
                id=trigger_id,
                tenant_id=tenant_id,
                agent_definition_id=definition_id,
                kind=ScheduledTriggerKind.CRON.value,
                cron_expr="*/5 * * * *",
                fire_at=None,
                event_filter=None,
                status=ScheduledTriggerStatus.ACTIVE.value,
                in_flight_policy=ScheduledTriggerInFlightPolicy.RESUME.value,
                next_fire_at=now,
                last_fired_at=None,
                created_by_sub="user-admin",
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(
            select(ScheduledTrigger).where(ScheduledTrigger.id == trigger_id)
        )
        row = result.scalar_one()

    assert row.id == trigger_id
    assert row.tenant_id == tenant_id
    assert row.agent_definition_id == definition_id
    assert row.kind == "cron"
    assert row.cron_expr == "*/5 * * * *"
    assert row.fire_at is None
    assert row.event_filter is None
    assert row.status == "active"
    assert row.in_flight_policy == "resume"
    assert row.next_fire_at is not None
    assert row.last_fired_at is None
    assert row.created_by_sub == "user-admin"


@pytest.mark.asyncio
async def test_one_off_trigger_round_trip_persists_every_field() -> None:
    """Insert a fully-populated one_off :class:`ScheduledTrigger`, all fields round-trip."""
    sessionmaker = get_sessionmaker()
    trigger_id = uuid.uuid4()
    fire_at = datetime.now(UTC)

    async with sessionmaker() as session:
        tenant_id, definition_id = await _seed_tenant_and_definition(session)
        session.add(
            ScheduledTrigger(
                id=trigger_id,
                tenant_id=tenant_id,
                agent_definition_id=definition_id,
                kind=ScheduledTriggerKind.ONE_OFF.value,
                cron_expr=None,
                fire_at=fire_at,
                event_filter=None,
                status=ScheduledTriggerStatus.ACTIVE.value,
                in_flight_policy=ScheduledTriggerInFlightPolicy.FAIL_INTO_AUDIT.value,
                next_fire_at=fire_at,
                created_by_sub="user-admin",
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(
            select(ScheduledTrigger).where(ScheduledTrigger.id == trigger_id)
        )
        row = result.scalar_one()

    assert row.kind == "one_off"
    assert row.cron_expr is None
    assert row.fire_at is not None
    assert row.event_filter is None
    assert row.in_flight_policy == "fail_into_audit"


@pytest.mark.asyncio
async def test_event_trigger_round_trip_persists_every_field() -> None:
    """Insert a fully-populated event :class:`ScheduledTrigger`, all fields round-trip."""
    sessionmaker = get_sessionmaker()
    trigger_id = uuid.uuid4()
    event_filter = {"connector": "github", "kind": "issue_opened", "labels": ["sev1"]}

    async with sessionmaker() as session:
        tenant_id, definition_id = await _seed_tenant_and_definition(session)
        session.add(
            ScheduledTrigger(
                id=trigger_id,
                tenant_id=tenant_id,
                agent_definition_id=definition_id,
                kind=ScheduledTriggerKind.EVENT.value,
                cron_expr=None,
                fire_at=None,
                event_filter=event_filter,
                status=ScheduledTriggerStatus.PAUSED.value,
                created_by_sub="user-admin",
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(
            select(ScheduledTrigger).where(ScheduledTrigger.id == trigger_id)
        )
        row = result.scalar_one()

    assert row.kind == "event"
    assert row.cron_expr is None
    assert row.fire_at is None
    assert row.event_filter == event_filter
    assert row.status == "paused"


# ---------------------------------------------------------------------------
# ORM defaults
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orm_defaults_fire_on_sqlite() -> None:
    """``id`` / ``status`` / ``in_flight_policy`` / ``created_at`` filled Python-side.

    The migration's PG-side server defaults (``gen_random_uuid()``,
    ``'active'``, ``'fail_into_audit'``, ``now()``) are no-ops on
    SQLite. The ORM defaults must fill them. A regression dropping any
    ORM default in favour of the migration's server default would
    surface here as a NOT NULL violation on SQLite.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id, definition_id = await _seed_tenant_and_definition(session)
        trigger = ScheduledTrigger(
            tenant_id=tenant_id,
            agent_definition_id=definition_id,
            kind=ScheduledTriggerKind.CRON.value,
            cron_expr="0 * * * *",
            created_by_sub="user-min",
        )
        session.add(trigger)
        await session.commit()
        seen_id = trigger.id
        seen_status = trigger.status
        seen_policy = trigger.in_flight_policy
        seen_created = trigger.created_at

    assert isinstance(seen_id, uuid.UUID)
    assert seen_status == ScheduledTriggerStatus.ACTIVE.value
    assert seen_policy == ScheduledTriggerInFlightPolicy.FAIL_INTO_AUDIT.value
    assert seen_created is not None

    # The nullable columns default to None when omitted.
    async with sessionmaker() as session:
        result = await session.execute(
            select(ScheduledTrigger).where(ScheduledTrigger.id == seen_id)
        )
        row = result.scalar_one()
    assert row.fire_at is None
    assert row.event_filter is None
    assert row.next_fire_at is None
    assert row.last_fired_at is None


# ---------------------------------------------------------------------------
# Closed-enum CHECK constraints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kind_check_rejects_unknown() -> None:
    """A ``kind`` outside the closed enum raises :class:`IntegrityError`."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id, definition_id = await _seed_tenant_and_definition(session)
        session.add(
            ScheduledTrigger(
                tenant_id=tenant_id,
                agent_definition_id=definition_id,
                kind="not-a-real-kind",
                cron_expr="* * * * *",
                created_by_sub="user-bad",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_status_check_rejects_unknown() -> None:
    """A ``status`` outside the closed enum raises :class:`IntegrityError`."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id, definition_id = await _seed_tenant_and_definition(session)
        session.add(
            ScheduledTrigger(
                tenant_id=tenant_id,
                agent_definition_id=definition_id,
                kind=ScheduledTriggerKind.CRON.value,
                cron_expr="* * * * *",
                status="archived",  # outside the closed enum
                created_by_sub="user-bad",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_in_flight_policy_check_rejects_unknown() -> None:
    """An ``in_flight_policy`` outside the closed enum raises :class:`IntegrityError`."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id, definition_id = await _seed_tenant_and_definition(session)
        session.add(
            ScheduledTrigger(
                tenant_id=tenant_id,
                agent_definition_id=definition_id,
                kind=ScheduledTriggerKind.CRON.value,
                cron_expr="* * * * *",
                in_flight_policy="retry_with_backoff",  # outside the closed enum
                created_by_sub="user-bad",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


# ---------------------------------------------------------------------------
# Discriminated-union CHECK constraint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cron_kind_requires_cron_expr() -> None:
    """A ``kind = 'cron'`` row with ``cron_expr IS NULL`` raises :class:`IntegrityError`."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id, definition_id = await _seed_tenant_and_definition(session)
        session.add(
            ScheduledTrigger(
                tenant_id=tenant_id,
                agent_definition_id=definition_id,
                kind=ScheduledTriggerKind.CRON.value,
                # cron_expr deliberately omitted -- breaks the discriminator.
                created_by_sub="user-bad",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_one_off_kind_rejects_cron_expr() -> None:
    """A ``kind = 'one_off'`` row with ``cron_expr`` populated fails the discriminator."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id, definition_id = await _seed_tenant_and_definition(session)
        session.add(
            ScheduledTrigger(
                tenant_id=tenant_id,
                agent_definition_id=definition_id,
                kind=ScheduledTriggerKind.ONE_OFF.value,
                cron_expr="* * * * *",  # not allowed under one_off
                fire_at=datetime.now(UTC),
                created_by_sub="user-bad",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_event_kind_requires_event_filter() -> None:
    """A ``kind = 'event'`` row with ``event_filter IS NULL`` fails the discriminator."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id, definition_id = await _seed_tenant_and_definition(session)
        session.add(
            ScheduledTrigger(
                tenant_id=tenant_id,
                agent_definition_id=definition_id,
                kind=ScheduledTriggerKind.EVENT.value,
                # event_filter deliberately omitted -- breaks the discriminator.
                created_by_sub="user-bad",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


# ---------------------------------------------------------------------------
# Foreign-key constraints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_fk_enforced() -> None:
    """A dangling ``tenant_id`` raises :class:`IntegrityError` under FK enforcement.

    Seeds the parent tenant + agent_definition rows *first* (the FK
    PRAGMA is off at that point so the helper's own
    Tenant->AgentDefinition insert sequence is unconstrained), then
    flips ``PRAGMA foreign_keys = ON`` and attempts the orphan insert
    -- the same staged shape :func:`_enable_sqlite_foreign_keys` uses
    in :mod:`tests.test_db_agent_run`.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        _, definition_id = await _seed_tenant_and_definition(session)
        await _enable_sqlite_foreign_keys(session)
        session.add(
            ScheduledTrigger(
                tenant_id=uuid.uuid4(),  # no such tenant row
                agent_definition_id=definition_id,
                kind=ScheduledTriggerKind.CRON.value,
                cron_expr="* * * * *",
                created_by_sub="user-orphan",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_agent_definition_fk_enforced() -> None:
    """A dangling ``agent_definition_id`` raises :class:`IntegrityError`."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id, _ = await _seed_tenant_and_definition(session)
        await _enable_sqlite_foreign_keys(session)
        session.add(
            ScheduledTrigger(
                tenant_id=tenant_id,
                agent_definition_id=uuid.uuid4(),  # no such definition row
                kind=ScheduledTriggerKind.CRON.value,
                cron_expr="* * * * *",
                created_by_sub="user-orphan",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


# ---------------------------------------------------------------------------
# Drift guards
# ---------------------------------------------------------------------------


def test_kinds_match_enum() -> None:
    """:data:`_SCHEDULED_TRIGGER_KINDS` mirrors :class:`ScheduledTriggerKind`."""
    assert set(_SCHEDULED_TRIGGER_KINDS) == {k.value for k in ScheduledTriggerKind}


def test_statuses_match_enum() -> None:
    """:data:`_SCHEDULED_TRIGGER_STATUSES` mirrors :class:`ScheduledTriggerStatus`."""
    assert set(_SCHEDULED_TRIGGER_STATUSES) == {s.value for s in ScheduledTriggerStatus}


def test_in_flight_policies_match_enum() -> None:
    """``_SCHEDULED_TRIGGER_IN_FLIGHT_POLICIES`` mirrors :class:`ScheduledTriggerInFlightPolicy`."""
    assert set(_SCHEDULED_TRIGGER_IN_FLIGHT_POLICIES) == {
        p.value for p in ScheduledTriggerInFlightPolicy
    }


def _load_migration_0020() -> object:
    """Load migration ``0020`` as a module via its file path.

    Alembic version files are digit-prefixed (``0020_create_scheduled_trigger``)
    and so are not importable as normal dotted modules. Loading by file
    path with :mod:`importlib.util` is the robust way to reach the
    migration's recorded literal tuples for the drift guards below.
    """
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parent.parent
        / "alembic"
        / "versions"
        / "0020_create_scheduled_trigger.py"
    )
    spec = importlib.util.spec_from_file_location("_migration_0020", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_kind_literals_match_model_enum() -> None:
    """The migration's frozen kind tuple matches the model enum."""
    migration = _load_migration_0020()
    assert set(migration._SCHEDULED_TRIGGER_KINDS) == {  # type: ignore[attr-defined]
        k.value for k in ScheduledTriggerKind
    }


def test_migration_status_literals_match_model_enum() -> None:
    """The migration's frozen status tuple matches the model enum."""
    migration = _load_migration_0020()
    assert set(migration._SCHEDULED_TRIGGER_STATUSES) == {  # type: ignore[attr-defined]
        s.value for s in ScheduledTriggerStatus
    }


def test_migration_in_flight_policy_literals_match_model_enum() -> None:
    """The migration's frozen in_flight_policy tuple matches the model enum."""
    migration = _load_migration_0020()
    assert set(migration._SCHEDULED_TRIGGER_IN_FLIGHT_POLICIES) == {  # type: ignore[attr-defined]
        p.value for p in ScheduledTriggerInFlightPolicy
    }
