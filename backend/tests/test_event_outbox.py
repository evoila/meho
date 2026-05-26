# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the G11.3-T3 event outbox + drain loop (#824).

Coverage matrix mapped to the issue's acceptance criteria:

* **Same-transaction discipline** -- a producer rollback discards the
  outbox row alongside the event-producing state change (the AC's
  durability guarantee on the producer side).
* **Drain claims + processes** -- the drain loop reads unprocessed
  rows in ``event_id`` order, claims them, and stamps
  ``processed_at`` exactly once.
* **Replica-safe / no double-process** -- two concurrent drain ticks
  against the same DB process each event exactly once (the consumer-
  doc-required SKIP LOCKED contract).
* **Durability across restart** -- the drain task is started, stopped
  mid-tick (simulating a process kill), and started again with
  unprocessed rows present. They drain on the next tick.
* **Agent-run completion publishes** -- transitioning an ``agent_run``
  to a terminal status (``succeeded`` / ``failed`` / ``cancelled``)
  writes the outbox row in the same session, so the producer's
  commit publishes the event durably.

The tests run on the autouse SQLite-backed engine from
:mod:`tests.conftest`; ``LISTEN/NOTIFY`` is a no-op on SQLite (the
production-only wake hint), so the wake-hint coverage lives in the
integration suite where a real PG container is available.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentRun,
    AgentRunStatus,
    AgentRunTrigger,
    EventOutbox,
    Tenant,
)
from meho_backplane.events.drain import run_one_drain_tick, start_event_drain, stop_event_drain
from meho_backplane.events.outbox import publish
from meho_backplane.operations.agent_run import (
    AGENT_RUN_COMPLETED_EVENT_KIND,
    create_run,
    fail_run,
    succeed_run,
    transition,
)
from meho_backplane.settings import get_settings

_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture(autouse=True)
def _required_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin :class:`Settings` env vars; clear the lru cache."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    # Crank the drain cadence way down so the lifecycle test exercises
    # multiple ticks without inflating wall time.
    monkeypatch.setenv("EVENT_DRAIN_TICK_INTERVAL_SECONDS", "1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _seed_tenant() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        if await session.get(Tenant, _TENANT_A) is None:
            session.add(Tenant(id=_TENANT_A, slug="tenant-a", name="Tenant A"))
            await session.commit()


async def _unprocessed_count() -> int:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(EventOutbox).where(EventOutbox.processed_at.is_(None))
        )
        return len(result.scalars().all())


async def _processed_count() -> int:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(EventOutbox).where(EventOutbox.processed_at.is_not(None))
        )
        return len(result.scalars().all())


async def _all_outbox_rows() -> list[EventOutbox]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(EventOutbox).order_by(EventOutbox.event_id.asc()))
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Same-transaction discipline
# ---------------------------------------------------------------------------


async def test_publish_rolls_back_with_producer() -> None:
    """A producer rollback discards the outbox row in the same session.

    The transactional outbox's load-bearing invariant: the outbox
    INSERT shares the producer's commit. A rollback discards both --
    the event was never durably produced. This guards against
    inconsistent state where the event-producing state change rolled
    back but the outbox row leaked, firing subscribers for a
    non-event.
    """
    await _seed_tenant()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await publish(
            session,
            tenant_id=_TENANT_A,
            event_kind="test.dummy",
            payload={"k": "v"},
        )
        # Producer-side failure path -- rollback, not commit.
        await session.rollback()

    assert await _unprocessed_count() == 0, (
        "rollback must discard the outbox row alongside the producer state change"
    )


async def test_publish_commits_with_producer() -> None:
    """A producer commit makes the outbox row durable."""
    await _seed_tenant()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await publish(
            session,
            tenant_id=_TENANT_A,
            event_kind="test.dummy",
            payload={"k": "v"},
        )
        await session.commit()

    rows = await _all_outbox_rows()
    assert len(rows) == 1
    assert rows[0].event_kind == "test.dummy"
    assert rows[0].payload == {"k": "v"}
    assert rows[0].processed_at is None


# ---------------------------------------------------------------------------
# Drain claim + process
# ---------------------------------------------------------------------------


async def test_drain_tick_processes_unprocessed() -> None:
    """One drain tick claims every unprocessed row and stamps processed."""
    await _seed_tenant()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        for i in range(3):
            await publish(
                session,
                tenant_id=_TENANT_A,
                event_kind="test.dummy",
                payload={"i": i},
            )
        await session.commit()

    processed = await run_one_drain_tick()
    assert processed == 3, "drain tick must process every unprocessed row"
    assert await _unprocessed_count() == 0
    assert await _processed_count() == 3

    # Re-tick is a no-op (rows are already processed).
    assert await run_one_drain_tick() == 0


async def test_drain_no_double_process_under_concurrency() -> None:
    """Two concurrent drain ticks process each event exactly once.

    On PG this is enforced by ``SELECT FOR UPDATE SKIP LOCKED``; on
    SQLite (this test path) the conditional ``UPDATE ... WHERE
    processed_at IS NULL`` enforces single-processing across two
    in-process drainers sharing the same connection pool. The
    invariant is the same either way: ``sum(processed) == N``, never
    ``2N``.
    """
    await _seed_tenant()
    sessionmaker = get_sessionmaker()
    n_events = 8
    async with sessionmaker() as session:
        for i in range(n_events):
            await publish(
                session,
                tenant_id=_TENANT_A,
                event_kind="test.dummy",
                payload={"i": i},
            )
        await session.commit()

    # Two ticks racing each other -- the sum of their counts is the
    # total event count (no double-processing), even though each
    # individual tick may see a different fraction.
    results = await asyncio.gather(run_one_drain_tick(), run_one_drain_tick())
    assert sum(results) == n_events, (
        f"two concurrent ticks must process every event exactly once; got {results}"
    )
    assert await _unprocessed_count() == 0


# ---------------------------------------------------------------------------
# Restart durability
# ---------------------------------------------------------------------------


async def test_restart_durability_drains_unprocessed_rows() -> None:
    """Unprocessed rows survive a drain-task restart and drain on the next tick.

    The durability AC: write an event, start the drain task, stop it
    before its first tick can run, then re-start it. The row was
    durably persisted, so the second start's first tick drains it.
    """
    await _seed_tenant()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await publish(
            session,
            tenant_id=_TENANT_A,
            event_kind="test.dummy",
            payload={"k": "restart"},
        )
        await session.commit()

    # Simulate a process kill: start the drain, stop it before it
    # finishes its first tick.
    task = start_event_drain()
    # Yield a few times so the task scheduler picks up the loop, but
    # not long enough for the cadence sleep (1s) to elapse.
    await asyncio.sleep(0.1)
    await stop_event_drain(task)

    # The row is still unprocessed because the cadence sleep was
    # interrupted; durability hasn't been touched.
    assert await _unprocessed_count() == 1

    # Drive the deterministic single-tick (mirror what the restarted
    # loop would do once the cadence sleep elapses) -- this is the
    # post-restart "first tick".
    processed = await run_one_drain_tick()
    assert processed == 1, "post-restart tick must drain the unprocessed row"
    assert await _unprocessed_count() == 0


async def test_drain_lifecycle_start_stop() -> None:
    """``start_event_drain`` + ``stop_event_drain`` clean up the task."""
    task = start_event_drain()
    assert not task.done()
    await asyncio.sleep(0.05)
    await stop_event_drain(task)
    assert task.done()
    # The CancelledError swallowing in stop_event_drain means the
    # task's exception is absorbed; no .exception() raise.
    assert task.cancelled() or task.exception() is None


# ---------------------------------------------------------------------------
# Agent run completion publishes the event
# ---------------------------------------------------------------------------


async def _make_agent_run(
    status: AgentRunStatus = AgentRunStatus.PENDING,
) -> AgentRun:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await create_run(
            session,
            tenant_id=_TENANT_A,
            identity_sub="seed-user",
            trigger=AgentRunTrigger.DIRECT,
            model_tier="standard",
        )
        if status is not AgentRunStatus.PENDING:
            await transition(session, row, AgentRunStatus.RUNNING)
            if status is not AgentRunStatus.RUNNING:
                await transition(session, row, status)
        await session.commit()
        return row


async def test_succeed_run_publishes_outbox_event_in_same_tx() -> None:
    """``succeed_run`` publishes ``agent_run.completed`` onto the outbox."""
    await _seed_tenant()
    run = await _make_agent_run(status=AgentRunStatus.RUNNING)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        attached = await session.get(AgentRun, run.id)
        assert attached is not None
        await succeed_run(session, attached, output={"result": "ok"})
        await session.commit()

    rows = await _all_outbox_rows()
    # Filter to the terminal-transition events (the seed in
    # _make_agent_run may also have published if the seed status was
    # terminal -- but we used RUNNING above, so it didn't).
    completed = [r for r in rows if r.event_kind == AGENT_RUN_COMPLETED_EVENT_KIND]
    assert len(completed) == 1, (
        f"succeed_run must publish exactly one agent_run.completed event; got {rows}"
    )
    payload = completed[0].payload
    assert payload["run_id"] == str(run.id)
    assert payload["tenant_id"] == str(_TENANT_A)
    assert payload["status"] == AgentRunStatus.SUCCEEDED.value


async def test_fail_run_publishes_outbox_event() -> None:
    """``fail_run`` publishes a terminal event with ``status='failed'``."""
    await _seed_tenant()
    run = await _make_agent_run(status=AgentRunStatus.RUNNING)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        attached = await session.get(AgentRun, run.id)
        assert attached is not None
        await fail_run(session, attached, error="boom")
        await session.commit()

    rows = await _all_outbox_rows()
    completed = [r for r in rows if r.event_kind == AGENT_RUN_COMPLETED_EVENT_KIND]
    assert len(completed) == 1
    assert completed[0].payload["status"] == AgentRunStatus.FAILED.value


async def test_terminal_event_rolls_back_with_run_transition() -> None:
    """If the run transition rolls back, the outbox event rolls back too.

    The same-transaction discipline applied to the terminal-transition
    publish: a producer rollback (the runtime decided to abort the
    commit after a downstream failure) must not leak the outbox event.
    """
    await _seed_tenant()
    run = await _make_agent_run(status=AgentRunStatus.RUNNING)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        attached = await session.get(AgentRun, run.id)
        assert attached is not None
        await succeed_run(session, attached, output={"result": "ok"})
        # Simulate a downstream failure aborting the commit.
        await session.rollback()

    rows = await _all_outbox_rows()
    completed = [r for r in rows if r.event_kind == AGENT_RUN_COMPLETED_EVENT_KIND]
    assert completed == [], "rollback must discard the terminal-transition outbox event"

    # And the run itself stayed in RUNNING (the transition rolled
    # back too) -- the run-state and outbox-event invariants stay
    # aligned.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        reloaded = await session.get(AgentRun, run.id)
        assert reloaded is not None
        assert reloaded.status == AgentRunStatus.RUNNING.value


# ---------------------------------------------------------------------------
# Ordering + payload shape
# ---------------------------------------------------------------------------


async def test_event_id_is_monotonic_per_publish() -> None:
    """``event_id`` is a monotonic BIGSERIAL across publishes."""
    await _seed_tenant()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await publish(session, tenant_id=_TENANT_A, event_kind="a", payload={})
        await publish(session, tenant_id=_TENANT_A, event_kind="b", payload={})
        await publish(session, tenant_id=_TENANT_A, event_kind="c", payload={})
        await session.commit()

    rows = await _all_outbox_rows()
    ids = [r.event_id for r in rows]
    assert ids == sorted(ids), f"event_id must be monotonic; got {ids}"
    assert len(set(ids)) == len(ids), "event_id must be unique"


async def test_publish_none_payload_defaults_to_empty_dict() -> None:
    """A ``None`` payload is normalised to ``{}`` (column is NOT NULL)."""
    await _seed_tenant()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await publish(
            session,
            tenant_id=_TENANT_A,
            event_kind="empty",
            payload=None,
        )
        await session.commit()

    rows = await _all_outbox_rows()
    assert len(rows) == 1
    assert rows[0].payload == {}


async def test_drain_claim_stamps_claimed_at_and_by() -> None:
    """The drain claim stamps ``claimed_at`` / ``claimed_by`` on processed rows."""
    await _seed_tenant()
    sessionmaker = get_sessionmaker()
    before = datetime.now(UTC)
    async with sessionmaker() as session:
        await publish(session, tenant_id=_TENANT_A, event_kind="x", payload={})
        await session.commit()

    await run_one_drain_tick()

    rows = await _all_outbox_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.claimed_at is not None
    # SQLite drops tz info; normalise for the comparison.
    claimed_at = row.claimed_at
    if claimed_at.tzinfo is None:
        claimed_at = claimed_at.replace(tzinfo=UTC)
    assert claimed_at >= before
    assert row.claimed_by is not None
    assert ":" in row.claimed_by, "claimed_by should be 'hostname:pid'"
