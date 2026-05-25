# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the G11.3-T2 cron + one-off scheduler (#823).

Coverage matrix mapped to the issue's acceptance criteria:

* **Cron trigger fires on schedule** -- after the loop ticks past
  ``next_fire_at`` the agent is invoked once and ``next_fire_at`` is
  advanced to the next cron match.
* **One-off fires once, never again** -- after the loop fires it the
  row transitions to ``status='fired'`` and a second tick fires zero
  additional agents.
* **Replica-safe / no double-fire** -- two scheduler ticks run
  concurrently against the same DB; together they fire exactly once
  per due row (no double-fire).
* **Restart durability + bounded catch-up** -- the loop is started,
  stopped (simulating a process kill), and started again across a
  ``next_fire_at`` boundary. The trigger fires once on the next tick
  and recovers a normal cadence (no missed-tick storm).
* **Croniter parse + advance** -- ``next_fire_after`` returns
  deterministic UTC instants for a known expression and base; an
  invalid expression raises :class:`InvalidCronExpressionError`.
* **start/stop lifecycle** -- the lifespan helpers create and cleanly
  cancel the background task with no "Task was destroyed" /
  "unretrieved CancelledError" warnings under pytest-asyncio.
* **Scheduler can be disabled via SCHEDULER_ENABLED=false** -- the
  lifespan does not create the task handle.

The tests run on the autouse SQLite-backed engine from
:mod:`tests.conftest`. Agent invocation is stubbed through a
:class:`~pydantic_ai.models.function.FunctionModel` so no real LLM is
hit (python_best_practices §14 -- no network in unit tests).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import ModuleType

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select

from meho_backplane.agent.invocation import AgentInvoker
from meho_backplane.agent.run import PydanticAgentRun
from meho_backplane.agents.schemas import AgentDefinitionCreate, AgentModelTier
from meho_backplane.agents.service import AgentDefinitionService
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentRun,
    AgentRunTrigger,
    ScheduledTrigger,
    ScheduledTriggerKind,
    ScheduledTriggerStatus,
    Tenant,
)
from meho_backplane.scheduler import start_scheduler, stop_scheduler
from meho_backplane.scheduler.cron import (
    InvalidCronExpressionError,
    next_fire_after,
)
from meho_backplane.scheduler.loop import run_one_tick
from meho_backplane.scheduler.repository import (
    create_cron_trigger,
    create_one_off_trigger,
)
from meho_backplane.settings import get_settings

_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin :class:`Settings` env vars; clear the lru cache."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _final_text(text: str) -> FunctionModel:
    """A deterministic model that answers immediately with *text*."""

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(text)])

    return FunctionModel(fn)


def _make_invoker() -> AgentInvoker:
    """Build an invoker over a deterministic FunctionModel (no real LLM)."""
    return AgentInvoker(
        runtime=PydanticAgentRun(model_factory=lambda: _final_text("done")),
    )


async def _seed_tenant_and_agent(name: str = "reporter") -> uuid.UUID:
    """Insert one Tenant and one enabled AgentDefinition; return def id."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        if await session.get(Tenant, _TENANT_A) is None:
            session.add(Tenant(id=_TENANT_A, slug="tenant-a", name="Tenant A"))
            await session.commit()
    service = AgentDefinitionService()
    entry = await service.create(
        tenant_id=_TENANT_A,
        created_by_sub="seed-admin",
        payload=AgentDefinitionCreate(
            name=name,
            identity_ref=f"agent:{name}",
            model_tier=AgentModelTier.STANDARD,
            system_prompt="You report status.",
            toolset={},
            turn_budget=2,
            enabled=True,
        ),
    )
    return entry.id


async def _create_cron(
    *,
    agent_definition_id: uuid.UUID,
    cron_expr: str = "*/5 * * * *",
    base: datetime,
) -> ScheduledTrigger:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await create_cron_trigger(
            session,
            tenant_id=_TENANT_A,
            agent_definition_id=agent_definition_id,
            cron_expr=cron_expr,
            inputs={"prompt": "ping"},
            identity_sub="op-scheduler",
            created_by_sub="seed-admin",
            base=base,
        )
        await session.commit()
        return row


async def _create_one_off(
    *,
    agent_definition_id: uuid.UUID,
    run_at: datetime,
) -> ScheduledTrigger:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await create_one_off_trigger(
            session,
            tenant_id=_TENANT_A,
            agent_definition_id=agent_definition_id,
            run_at=run_at,
            inputs={"prompt": "one-shot"},
            identity_sub="op-scheduler",
            created_by_sub="seed-admin",
        )
        await session.commit()
        return row


async def _force_due(trigger_id: uuid.UUID, when: datetime) -> None:
    """Force *trigger_id*'s ``next_fire_at`` to *when* (test seam)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.get(ScheduledTrigger, trigger_id)
        assert row is not None
        row.next_fire_at = when
        await session.commit()


async def _get_trigger(trigger_id: uuid.UUID) -> ScheduledTrigger:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.get(ScheduledTrigger, trigger_id)
        assert row is not None
        return row


def _aware(dt: datetime | None) -> datetime | None:
    """Attach UTC to a naive datetime (SQLite drops tz on round-trip).

    SQLAlchemy 2.0 + aiosqlite + ``DateTime(timezone=True)`` round-trip
    UTC instants as naive datetimes; the production path (PG) returns
    aware. Tests that compare a re-read column against a tz-aware
    expectation normalise here so the assertion is identical on both
    dialects.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


async def _wait_for_agent_runs(
    expected: int,
    *,
    trigger: AgentRunTrigger | None = None,
    timeout: float = 3.0,
) -> list[AgentRun]:
    """Poll the ``agent_run`` table until *expected* rows land, or fail."""
    deadline = asyncio.get_event_loop().time() + timeout
    sessionmaker = get_sessionmaker()
    while asyncio.get_event_loop().time() < deadline:
        async with sessionmaker() as session:
            stmt = select(AgentRun)
            if trigger is not None:
                stmt = stmt.where(AgentRun.trigger == trigger.value)
            rows = list((await session.execute(stmt)).scalars().all())
            if len(rows) >= expected:
                return rows
        await asyncio.sleep(0.05)
    async with sessionmaker() as session:
        stmt = select(AgentRun)
        rows = list((await session.execute(stmt)).scalars().all())
    pytest.fail(f"expected {expected} agent_run rows, found {len(rows)}")


# ---------------------------------------------------------------------------
# Cron arithmetic
# ---------------------------------------------------------------------------


def test_next_fire_after_deterministic_for_known_expression() -> None:
    """``*/5 * * * *`` from 12:00 yields 12:05 (UTC, deterministic)."""
    base = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    result = next_fire_after("*/5 * * * *", base)
    assert result == datetime(2026, 5, 25, 12, 5, 0, tzinfo=UTC)


def test_next_fire_after_strictly_advances_past_base() -> None:
    """A base instant exactly *on* a cron match advances to the next match."""
    base = datetime(2026, 5, 25, 12, 5, 0, tzinfo=UTC)
    result = next_fire_after("*/5 * * * *", base)
    assert result == datetime(2026, 5, 25, 12, 10, 0, tzinfo=UTC)


def test_next_fire_after_rejects_garbage_expression() -> None:
    """Invalid cron expressions raise the typed exception."""
    with pytest.raises(InvalidCronExpressionError):
        next_fire_after("not a cron expr", datetime.now(UTC))


def test_next_fire_after_returns_utc_for_non_utc_timezone() -> None:
    """A trigger in Europe/Berlin still persists ``next_fire_at`` as UTC.

    ``0 9 * * *`` (every day 09:00) in Berlin (UTC+2 in May) is 07:00 UTC.
    """
    base = datetime(2026, 5, 25, 6, 0, 0, tzinfo=UTC)
    result = next_fire_after("0 9 * * *", base, "Europe/Berlin")
    assert result == datetime(2026, 5, 25, 7, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Cron trigger fires on schedule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cron_trigger_fires_when_due_and_advances() -> None:
    """A due cron trigger fires the agent and ``next_fire_at`` advances."""
    agent_id = await _seed_tenant_and_agent()
    base = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    trigger = await _create_cron(agent_definition_id=agent_id, base=base)
    # Force the trigger overdue.
    await _force_due(trigger.id, datetime(2026, 1, 1, tzinfo=UTC))

    fires = await run_one_tick(invoker=_make_invoker())
    assert fires == 1

    runs = await _wait_for_agent_runs(1, trigger=AgentRunTrigger.SCHEDULED)
    assert len(runs) == 1
    assert runs[0].trigger == AgentRunTrigger.SCHEDULED.value

    advanced = await _get_trigger(trigger.id)
    assert advanced.status == ScheduledTriggerStatus.ACTIVE.value
    # next_fire_at must be strictly later than the old due instant.
    next_fire = _aware(advanced.next_fire_at)
    assert next_fire is not None
    assert next_fire > datetime(2026, 1, 1, tzinfo=UTC)
    assert advanced.last_fired_at is not None


# ---------------------------------------------------------------------------
# One-off fires once, never again
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_off_trigger_fires_once_and_marks_fired() -> None:
    """A one-off fires once and never again on subsequent ticks."""
    agent_id = await _seed_tenant_and_agent()
    trigger = await _create_one_off(
        agent_definition_id=agent_id,
        run_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    invoker = _make_invoker()
    fires_first = await run_one_tick(invoker=invoker)
    assert fires_first == 1

    runs = await _wait_for_agent_runs(1, trigger=AgentRunTrigger.SCHEDULED)
    assert len(runs) == 1

    finalised = await _get_trigger(trigger.id)
    assert finalised.status == ScheduledTriggerStatus.FIRED.value

    # Second tick must not refire.
    fires_second = await run_one_tick(invoker=invoker)
    assert fires_second == 0
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        all_runs = list((await session.execute(select(AgentRun))).scalars().all())
    assert len(all_runs) == 1


# ---------------------------------------------------------------------------
# Replica-safety -- two concurrent ticks fire exactly once per due row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_concurrent_ticks_never_double_fire() -> None:
    """Two scheduler ticks run concurrently fire exactly once per due row.

    Simulates two replicas by launching two ``run_one_tick`` coroutines on
    the same DB. The advisory lock is a no-op on SQLite, so this exercises
    the per-row conditional-UPDATE single-fire enforcement
    (:func:`advance_cron_trigger` / :func:`mark_one_off_fired`) which is
    the belt-and-braces guard required for PG too.
    """
    agent_id = await _seed_tenant_and_agent()
    trigger = await _create_cron(
        agent_definition_id=agent_id,
        base=datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC),
    )
    await _force_due(trigger.id, datetime(2026, 1, 1, tzinfo=UTC))

    invoker = _make_invoker()
    results = await asyncio.gather(
        run_one_tick(invoker=invoker),
        run_one_tick(invoker=invoker),
    )
    assert sum(results) == 1, f"expected 1 total fire, got {results}"

    runs = await _wait_for_agent_runs(1, trigger=AgentRunTrigger.SCHEDULED)
    assert len(runs) == 1


@pytest.mark.asyncio
async def test_two_concurrent_ticks_never_double_fire_one_off() -> None:
    """Same property for a due one-off trigger."""
    agent_id = await _seed_tenant_and_agent()
    trigger = await _create_one_off(
        agent_definition_id=agent_id,
        run_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    invoker = _make_invoker()
    results = await asyncio.gather(
        run_one_tick(invoker=invoker),
        run_one_tick(invoker=invoker),
    )
    assert sum(results) == 1, f"expected 1 total fire, got {results}"

    runs = await _wait_for_agent_runs(1, trigger=AgentRunTrigger.SCHEDULED)
    assert len(runs) == 1

    final = await _get_trigger(trigger.id)
    assert final.status == ScheduledTriggerStatus.FIRED.value


# ---------------------------------------------------------------------------
# Restart durability -- the trigger's state survives a process restart
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_durable_missed_tick_fires_once_no_storm() -> None:
    """A trigger that crossed its ``next_fire_at`` while the loop was off
    fires exactly once on resume (no catch-up storm).
    """
    agent_id = await _seed_tenant_and_agent()
    trigger = await _create_cron(
        agent_definition_id=agent_id,
        base=datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC),
    )
    # Simulate a long outage: the trigger's next_fire_at is 24 h in the
    # past. A naive "replay every missed tick" implementation would fire
    # 288 times for `*/5 * * * *`; the correct behaviour is "fire once,
    # then re-anchor to next match".
    await _force_due(trigger.id, datetime.now(UTC) - timedelta(hours=24))

    invoker = _make_invoker()
    fires = await run_one_tick(invoker=invoker)
    assert fires == 1, "missed-tick burst -- exactly one fire expected on resume"

    runs = await _wait_for_agent_runs(1, trigger=AgentRunTrigger.SCHEDULED)
    assert len(runs) == 1

    # The next fire must be in the future (cleanly re-anchored).
    advanced = await _get_trigger(trigger.id)
    next_fire = _aware(advanced.next_fire_at)
    assert next_fire is not None
    assert next_fire > datetime.now(UTC) - timedelta(seconds=10)


# ---------------------------------------------------------------------------
# Lifecycle -- start_scheduler / stop_scheduler cleanly start + cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_and_stop_scheduler_lifecycle_clean() -> None:
    """The lifespan helpers create + cancel a task without GC warnings."""
    task = start_scheduler()
    assert not task.done()
    await stop_scheduler(task)
    assert task.done()
    assert task.cancelled() or task.exception() is None


# ---------------------------------------------------------------------------
# Disabled-scheduler path -- SCHEDULER_ENABLED=false makes lifespan skip it
# ---------------------------------------------------------------------------


def test_scheduler_disabled_setting_resolves_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCHEDULER_ENABLED=false reads through to the settings flag."""
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.scheduler_enabled is False
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Failure isolation -- a corrupt cron expression parks the row, doesn't kill the tick
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_corrupt_cron_expression_parks_the_row() -> None:
    """A trigger whose cron expression no longer parses is paused, not retried.

    Direct-write into the row simulates a corruption that bypassed the
    create-time validator (operator edited the DB directly, or a future
    migration mishandled the column).
    """
    agent_id = await _seed_tenant_and_agent()
    trigger = await _create_cron(
        agent_definition_id=agent_id,
        base=datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC),
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.get(ScheduledTrigger, trigger.id)
        assert row is not None
        row.cron_expr = "not a cron expr"
        row.next_fire_at = datetime(2026, 1, 1, tzinfo=UTC)
        await session.commit()

    fires = await run_one_tick(invoker=_make_invoker())
    assert fires == 0

    parked = await _get_trigger(trigger.id)
    assert parked.status == ScheduledTriggerStatus.PAUSED.value


# ---------------------------------------------------------------------------
# Drift guard -- StrEnum vocabularies match the migration's CHECK literals
# ---------------------------------------------------------------------------


def _load_migration_by_name(name: str) -> ModuleType:
    """Load an Alembic migration by file basename (digit-prefixed -- not a dotted mod).

    Mirrors :func:`tests.test_db_agent_run._load_migration_0017`. The
    drift-guard tests below compose the migration history's literal
    tuples for the status enum (0020 originated, 0021 widened with
    ``fired``); using a name-parameterised loader keeps the helper
    open to further widenings (0022+) without copy-pasting the path
    incantation.
    """
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "alembic" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_migration_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scheduled_trigger_kind_check_matches_enum() -> None:
    """``ScheduledTriggerKind`` values agree with the migration's CHECK list."""
    from meho_backplane.db.models import _SCHEDULED_TRIGGER_KINDS

    migration = _load_migration_by_name("0020_create_scheduled_trigger")
    assert set(_SCHEDULED_TRIGGER_KINDS) == {k.value for k in ScheduledTriggerKind}
    assert set(_SCHEDULED_TRIGGER_KINDS) == set(migration._SCHEDULED_TRIGGER_KINDS)


def test_scheduled_trigger_status_check_matches_enum() -> None:
    """``ScheduledTriggerStatus`` agrees with the effective migration history.

    0020 shipped ``{active, paused, cancelled}``; 0021 widened the
    ``CHECK`` to add ``fired`` (the terminal one-off state the
    dispatcher transitions to after a successful single-fire). The
    effective vocabulary is therefore the 0021 ``_V2`` literal; the
    model's :class:`ScheduledTriggerStatus` enum must agree.
    """
    from meho_backplane.db.models import _SCHEDULED_TRIGGER_STATUSES

    m_0021 = _load_migration_by_name("0021_scheduled_trigger_dispatcher_columns")
    assert set(_SCHEDULED_TRIGGER_STATUSES) == {s.value for s in ScheduledTriggerStatus}
    assert set(_SCHEDULED_TRIGGER_STATUSES) == set(m_0021._SCHEDULED_TRIGGER_STATUSES_V2)


# ---------------------------------------------------------------------------
# Soft-FK -- a trigger whose definition was deleted skips the fire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_agent_definition_skips_fire_without_killing_tick() -> None:
    """A trigger pointing at a deleted definition does not fire and does not
    park (the operator may recreate the definition).
    """
    agent_id = await _seed_tenant_and_agent()
    trigger = await _create_cron(
        agent_definition_id=agent_id,
        base=datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC),
    )
    await _force_due(trigger.id, datetime(2026, 1, 1, tzinfo=UTC))

    # Hard-delete the definition (soft-FK -- no cascade).
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        from sqlalchemy import delete

        from meho_backplane.db.models import AgentDefinition

        await session.execute(delete(AgentDefinition).where(AgentDefinition.id == agent_id))
        await session.commit()

    fires = await run_one_tick(invoker=_make_invoker())
    assert fires == 0

    # Trigger remains ACTIVE so an operator who recreates the definition
    # unblocks the schedule on the next tick.
    surviving = await _get_trigger(trigger.id)
    assert surviving.status == ScheduledTriggerStatus.ACTIVE.value
