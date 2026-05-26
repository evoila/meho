# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the :class:`AgentRun` ORM model + its closed enums.

Initiative #802 (G11.1 Agent runtime), Task #813 (T6). The ``agent_run``
table is one row per LLM-agent invocation hosted in MEHO's process; its
``id`` doubles as the ``agent_session_id`` lineage key G11.4/C2 binds into
per-tool-call audit rows.

Coverage matrix
---------------

* **Round-trip persists every field.** Insert a fully-populated
  :class:`AgentRun`, read it back, every column round-trips.
* **ORM defaults fire on SQLite.** ``id`` / ``status`` / ``turns`` /
  ``created_at`` populated Python-side where the migration's PG server
  defaults are no-ops; the nullable columns default to ``None``.
* **Status CHECK rejects unknown.** A ``status`` outside
  :class:`AgentRunStatus` raises :class:`IntegrityError` -- the DB-layer
  closed-enum guard.
* **Trigger CHECK rejects unknown.** Same for ``trigger`` /
  :class:`AgentRunTrigger`.
* **tenant FK enforced.** An ``agent_run`` row with a dangling
  ``tenant_id`` raises :class:`IntegrityError` under
  ``PRAGMA foreign_keys = ON``.
* **Drift guards.** The model enums and the live ``CHECK`` constraints
  agree; the migration's frozen literal tuples match the model enums.

The tests run synchronously against ``sqlite+aiosqlite`` via the shared
engine cache the autouse ``_default_database_url`` fixture in
:mod:`tests.conftest` pre-migrates to head. PG-real shape parity is
covered by the testcontainers replay suite in
:mod:`tests.test_migration_rollback`. This file stays Docker-free so the
always-on gate asserts the ORM contract on every PR.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    _AGENT_RUN_IN_FLIGHT_POLICIES,
    _AGENT_RUN_STATUSES,
    _AGENT_RUN_TRIGGERS,
    AgentRun,
    AgentRunStatus,
    AgentRunTrigger,
    ScheduledTriggerInFlightPolicy,
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

    SQLite ships with FK enforcement disabled by default
    (sqlite.org/foreignkeys.html §2). Without this PRAGMA, the
    FK-violation :class:`IntegrityError` the tenant-FK test expects would
    silently no-op. The shared engine uses :class:`StaticPool` on SQLite
    so a single connection backs every checkout in this test process.
    Mirrors the helper in :mod:`tests.test_topology_schema`.
    """
    await session.execute(text("PRAGMA foreign_keys = ON"))


async def _seed_tenant(session: AsyncSession, *, slug: str = "rdc-internal") -> uuid.UUID:
    """Return the tenant row's id, inserting it on the first call.

    ``agent_run.tenant_id`` carries a real FK to :class:`Tenant`, so the
    per-test setup needs a parent tenant to avoid spurious
    ``IntegrityError`` under PRAGMA foreign_keys=ON.

    The look-up-then-insert shape is defence-in-depth: migration
    ``0028`` seeds the ``default`` tenant into the per-worker schema
    template (:func:`tests.conftest._schema_template_db`) -- after
    G0.13-T7 (#1137) generalised the seed from ``rdc-internal`` to
    ``default``. With this helper's default ``slug='rdc-internal'``
    the look-up returns ``None`` and the helper inserts a fresh row;
    callers passing ``slug='default'`` (none today) would land on the
    seeded row instead. Returning the seeded row's id when the slug
    pre-exists keeps the helper compatible with both shapes.
    """
    existing: uuid.UUID | None = await session.scalar(
        select(Tenant.id).where(Tenant.slug == slug),
    )
    if existing is not None:
        return existing
    tenant_id = uuid.uuid4()
    session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
    await session.commit()
    return tenant_id


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_run_round_trip_persists_every_field() -> None:
    """Insert a fully-populated :class:`AgentRun`, read it back, all fields match."""
    sessionmaker = get_sessionmaker()
    run_id = uuid.uuid4()
    definition_id = uuid.uuid4()
    parent_id = uuid.uuid4()
    now = datetime.now(UTC)

    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        session.add(
            AgentRun(
                id=run_id,
                agent_definition_id=definition_id,
                tenant_id=tenant_id,
                identity_sub="user-123",
                identity_act="agent-triage",
                trigger=AgentRunTrigger.SCHEDULED.value,
                model_tier="deep",
                provider="anthropic",
                model="claude-opus-4",
                status=AgentRunStatus.SUCCEEDED.value,
                turns=7,
                cost=Decimal("0.123456"),
                output={"summary": "all green", "issues": []},
                error=None,
                parent_run_id=parent_id,
                created_at=now,
                started_at=now,
                ended_at=now,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(select(AgentRun).where(AgentRun.id == run_id))
        row = result.scalar_one()

    assert row.id == run_id
    assert row.agent_definition_id == definition_id
    assert row.tenant_id == tenant_id
    assert row.identity_sub == "user-123"
    assert row.identity_act == "agent-triage"
    assert row.trigger == "scheduled"
    assert row.model_tier == "deep"
    assert row.provider == "anthropic"
    assert row.model == "claude-opus-4"
    assert row.status == "succeeded"
    assert row.turns == 7
    assert row.cost == Decimal("0.123456")
    assert row.output == {"summary": "all green", "issues": []}
    assert row.error is None
    assert row.parent_run_id == parent_id
    assert row.created_at.replace(tzinfo=None) == now.replace(tzinfo=None)
    assert row.started_at is not None
    assert row.ended_at is not None


@pytest.mark.asyncio
async def test_agent_run_orm_defaults_fire_on_sqlite() -> None:
    """``id`` / ``status`` / ``turns`` / ``created_at`` filled Python-side; nullables None.

    The migration's PG-side server defaults (``gen_random_uuid()``,
    ``'pending'``, ``0``, ``now()``) are no-ops on SQLite. The ORM
    defaults must fill them. A regression dropping any ORM default in
    favour of the migration's server default would surface here as a
    NOT NULL violation on SQLite.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        run = AgentRun(
            tenant_id=tenant_id,
            identity_sub="user-min",
            trigger=AgentRunTrigger.DIRECT.value,
            model_tier="cheap",
        )
        session.add(run)
        await session.commit()
        seen_id = run.id
        seen_status = run.status
        seen_turns = run.turns
        seen_created = run.created_at

    assert isinstance(seen_id, uuid.UUID)
    assert seen_status == AgentRunStatus.PENDING.value
    assert seen_turns == 0
    assert seen_created is not None

    # The nullable columns default to None when omitted.
    async with sessionmaker() as session:
        result = await session.execute(select(AgentRun).where(AgentRun.id == seen_id))
        row = result.scalar_one()
    assert row.agent_definition_id is None
    assert row.identity_act is None
    assert row.provider is None
    assert row.model is None
    assert row.cost is None
    assert row.output is None
    assert row.error is None
    assert row.parent_run_id is None
    assert row.started_at is None
    assert row.ended_at is None


# ---------------------------------------------------------------------------
# CHECK constraints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_run_status_check_rejects_unknown() -> None:
    """A ``status`` outside the closed enum raises :class:`IntegrityError`."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        session.add(
            AgentRun(
                tenant_id=tenant_id,
                identity_sub="user-bad",
                trigger=AgentRunTrigger.DIRECT.value,
                model_tier="cheap",
                status="not-a-real-status",  # outside the closed enum
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_agent_run_trigger_check_rejects_unknown() -> None:
    """A ``trigger`` outside the closed enum raises :class:`IntegrityError`."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        session.add(
            AgentRun(
                tenant_id=tenant_id,
                identity_sub="user-bad",
                trigger="not-a-real-trigger",  # outside the closed enum
                model_tier="cheap",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_agent_run_tenant_fk_enforced() -> None:
    """A dangling ``tenant_id`` raises :class:`IntegrityError` under FK enforcement."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await _enable_sqlite_foreign_keys(session)
        session.add(
            AgentRun(
                tenant_id=uuid.uuid4(),  # no such tenant row
                identity_sub="user-orphan",
                trigger=AgentRunTrigger.DIRECT.value,
                model_tier="cheap",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


# ---------------------------------------------------------------------------
# Drift guards
# ---------------------------------------------------------------------------


def test_status_kinds_match_enum() -> None:
    """:data:`_AGENT_RUN_STATUSES` mirrors :class:`AgentRunStatus`.

    The model-side tuple feeds the DB ``CHECK`` constraint; equality with
    the enum is the drift guard that keeps the constraint and the enum in
    lock-step.
    """
    assert set(_AGENT_RUN_STATUSES) == {s.value for s in AgentRunStatus}


def test_trigger_kinds_match_enum() -> None:
    """:data:`_AGENT_RUN_TRIGGERS` mirrors :class:`AgentRunTrigger`."""
    assert set(_AGENT_RUN_TRIGGERS) == {t.value for t in AgentRunTrigger}


def _load_migration_0017() -> object:
    """Load migration ``0017`` as a module via its file path.

    Alembic version files are digit-prefixed (``0017_create_agent_run``)
    and so are not importable as normal dotted modules. Loading by file
    path with :mod:`importlib.util` is the robust way to reach the
    migration's recorded literal tuples for the drift guard below.
    """
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parent.parent / "alembic" / "versions" / "0017_create_agent_run.py"
    )
    spec = importlib.util.spec_from_file_location("_migration_0017", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_status_literals_match_model_enum() -> None:
    """The migration's frozen status tuple matches the model enum.

    Migration ``0017`` records the status vocabulary as a self-contained
    literal tuple (not an import) so its DDL is a frozen snapshot. This
    guard fails if the model enum is widened without updating the
    migration's recorded ``CHECK`` body -- the exact drift the lock-step
    discipline exists to catch.
    """
    migration = _load_migration_0017()

    assert set(migration._AGENT_RUN_STATUSES) == {s.value for s in AgentRunStatus}  # type: ignore[attr-defined]


def test_migration_trigger_literals_match_model_enum() -> None:
    """The migration's frozen trigger tuple matches the model enum."""
    migration = _load_migration_0017()

    assert set(migration._AGENT_RUN_TRIGGERS) == {t.value for t in AgentRunTrigger}  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Lease / heartbeat / in_flight_policy columns (T4 #825)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_run_lease_columns_round_trip() -> None:
    """Lease + in_flight_policy columns round-trip through the ORM.

    T4 #825. ``lease_owner`` / ``lease_expires_at`` are nullable
    side-effect columns the lifecycle service writes; ``in_flight_policy``
    is NOT NULL with a server default. All three persist verbatim
    through an insert + read cycle.
    """
    sessionmaker = get_sessionmaker()
    run_id = uuid.uuid4()
    lease_until = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        session.add(
            AgentRun(
                id=run_id,
                tenant_id=tenant_id,
                identity_sub="user-lease",
                trigger=AgentRunTrigger.SCHEDULED.value,
                model_tier="deep",
                status=AgentRunStatus.RUNNING.value,
                lease_owner="meho-backplane-pod-3:pid-42",
                lease_expires_at=lease_until,
                in_flight_policy=ScheduledTriggerInFlightPolicy.RESUME.value,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(select(AgentRun).where(AgentRun.id == run_id))
        row = result.scalar_one()

    assert row.lease_owner == "meho-backplane-pod-3:pid-42"
    assert row.lease_expires_at is not None
    # SQLite drops tz; compare wall-clock.
    assert row.lease_expires_at.replace(tzinfo=None) == lease_until.replace(tzinfo=None)
    assert row.in_flight_policy == "resume"


@pytest.mark.asyncio
async def test_agent_run_in_flight_policy_default_is_fail_into_audit() -> None:
    """The ORM default for ``in_flight_policy`` is ``'fail_into_audit'``.

    T4 #825. The consumer doc accepts ``fail_into_audit`` as the
    default outcome -- a run that does not opt into ``resume`` ends
    up failed in the audit log. The server-side DEFAULT in the
    migration fires for raw INSERTs; the ORM-side default fires for
    SQLAlchemy ``add()`` calls. Both must produce the same value.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        run = AgentRun(
            tenant_id=tenant_id,
            identity_sub="user-default-policy",
            trigger=AgentRunTrigger.DIRECT.value,
            model_tier="cheap",
        )
        session.add(run)
        await session.commit()
        seen_policy = run.in_flight_policy
        seen_lease_owner = run.lease_owner
        seen_lease_expires_at = run.lease_expires_at

    assert seen_policy == ScheduledTriggerInFlightPolicy.FAIL_INTO_AUDIT.value
    # Lease columns default to None (no worker has claimed yet).
    assert seen_lease_owner is None
    assert seen_lease_expires_at is None


@pytest.mark.asyncio
async def test_agent_run_in_flight_policy_check_rejects_unknown() -> None:
    """A policy outside the closed enum raises :class:`IntegrityError`.

    T4 #825 -- the DB-layer closed-enum guard. Same pattern as the
    ``status`` / ``trigger`` constraints.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        session.add(
            AgentRun(
                tenant_id=tenant_id,
                identity_sub="user-bad-policy",
                trigger=AgentRunTrigger.DIRECT.value,
                model_tier="cheap",
                in_flight_policy="retry_with_backoff",  # outside the closed enum
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


# ---------------------------------------------------------------------------
# Drift guards (T4 #825 -- in_flight_policy column)
# ---------------------------------------------------------------------------


def test_in_flight_policy_kinds_match_scheduled_trigger_enum() -> None:
    """:data:`_AGENT_RUN_IN_FLIGHT_POLICIES` mirrors :class:`ScheduledTriggerInFlightPolicy`.

    The per-run column's vocabulary is *the same* closed enum as the
    trigger's policy column -- the run row carries a snapshot of the
    trigger's value at run-start. The agent-run-side tuple is defined
    independently (because :class:`ScheduledTriggerInFlightPolicy` lives
    further down in the model file -- see the comment on
    :data:`_AGENT_RUN_IN_FLIGHT_POLICIES`); this guard catches drift.
    """
    assert set(_AGENT_RUN_IN_FLIGHT_POLICIES) == {p.value for p in ScheduledTriggerInFlightPolicy}


def _load_migration_0026() -> object:
    """Load migration ``0026`` as a module via its file path.

    Same shape as :func:`_load_migration_0017` -- digit-prefixed
    filename, not importable as a dotted module.
    """
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parent.parent
        / "alembic"
        / "versions"
        / "0026_add_agent_run_lease_reaper.py"
    )
    spec = importlib.util.spec_from_file_location("_migration_0026", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0026_in_flight_policy_literals_match_scheduled_trigger_enum() -> None:
    """Migration ``0026``'s frozen policy tuple matches :class:`ScheduledTriggerInFlightPolicy`.

    T4 #825. The migration records the vocabulary as a literal tuple
    (not an import) so its DDL is a frozen snapshot; equality with the
    enum is the drift guard that keeps the CHECK constraint and the
    enum in lock-step.
    """
    migration = _load_migration_0026()

    assert set(migration._AGENT_RUN_IN_FLIGHT_POLICIES) == {  # type: ignore[attr-defined]
        p.value for p in ScheduledTriggerInFlightPolicy
    }
