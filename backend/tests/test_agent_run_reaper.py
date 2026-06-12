# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the in-flight ``agent_run`` reaper.

Initiative #804 (G11.3 Scheduler), Task #825 (T4). The reaper in
:mod:`meho_backplane.agent.reaper` scans for ``status='running' AND
lease_expires_at < now()`` on a fixed cadence and applies
:attr:`AgentRun.in_flight_policy` (``fail_into_audit`` -> terminal
``failed`` + audit row; ``resume`` -> clear lease + audit row,
dispatcher re-claims).

Acceptance criterion (T4 #825):

> A run killed mid-flight ends in a terminal, audited state: either
> resumed to completion or ``failed`` with an interruption reason --
> never silently lost.

These tests fixture a run directly into the DB (no dispatcher
dependency on T2 #823 yet), back-date its ``lease_expires_at`` to
simulate a dead worker, then run one reaper tick and assert the
policy outcome + the audit row.

Tests run synchronously against the ``sqlite+aiosqlite`` engine the
autouse ``_default_database_url`` fixture pre-migrates to head.
Advisory-lock single-flighting is a PG-only concern; the SQLite
test path treats the lock as a no-op (the production guard is
covered by the integration testcontainers suite a follow-up Task
adds when DBOS-vs-roll-our-own is settled enterprise-wide).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.agent.reaper import (
    AGENT_RUN_REAPER_INTERRUPTION_REASON,
    _run_one_tick,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentRun,
    AgentRunStatus,
    AgentRunTrigger,
    AuditLog,
    ScheduledTriggerInFlightPolicy,
    Tenant,
)
from meho_backplane.operations.agent_run import (
    claim_lease,
    create_run,
    transition,
)
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module.

    The autouse ``_default_database_url`` fixture only pins
    ``DATABASE_URL``; Keycloak/Vault knobs come from each test file.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _seed_tenant(session: AsyncSession, *, slug: str = "rdc-internal") -> uuid.UUID:
    """Return the tenant row's id, inserting it on the first call.

    Same shape as :func:`tests.test_agent_run_lifecycle._seed_tenant`
    -- duplicated rather than imported because cross-module fixture
    imports add maintenance cost without test-discovery benefit.
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


async def _make_running_run_with_expired_lease(
    session: AsyncSession,
    *,
    policy: ScheduledTriggerInFlightPolicy,
    lease_age_seconds: int = 120,
    owner: str = "worker-died",
) -> uuid.UUID:
    """Insert a running ``agent_run`` whose lease expired *lease_age_seconds* ago.

    Returns the run's id. The expired lease + ``running`` status is
    exactly what the reaper's claim query selects.

    Each call uses a fresh tenant slug so xdist parallel runners do
    not collide on the seeded ``rdc-internal`` row -- the per-run
    tenant slug pattern mirrors :mod:`tests.test_agent_run_lifecycle`'s
    ``_make_run``.
    """
    tenant_id = await _seed_tenant(session, slug=f"t-{uuid.uuid4().hex[:8]}")
    run = await create_run(
        session,
        tenant_id=tenant_id,
        identity_sub="user-killed",
        trigger=AgentRunTrigger.SCHEDULED,
        model_tier="deep",
    )
    # Snapshot the policy on the run (T2/T3's run-start handshake).
    run.in_flight_policy = policy.value
    # Claim + walk to running, then back-date the lease so the next
    # reaper tick sees it.
    await claim_lease(session, run, owner=owner, ttl_seconds=60)
    await transition(session, run, AgentRunStatus.RUNNING)
    run.lease_expires_at = datetime.now(UTC) - timedelta(seconds=lease_age_seconds)
    await session.commit()
    return run.id


# ---------------------------------------------------------------------------
# Acceptance criterion: no silently-lost run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_restart_run_with_fail_into_audit_ends_failed_and_audited() -> None:
    """Acceptance: a killed run with ``fail_into_audit`` policy ends ``failed`` + audit row.

    T4 #825 acceptance criterion. The run was alive (``status='running'``
    with a lease), the worker died (lease expired in the past), one
    reaper tick runs, the row ends in a terminal ``failed`` state with
    a stable interruption error, and an audit row is written
    pointing at the run.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        run_id = await _make_running_run_with_expired_lease(
            session,
            policy=ScheduledTriggerInFlightPolicy.FAIL_INTO_AUDIT,
        )

    await _run_one_tick()

    async with sessionmaker() as session:
        run = await session.get(AgentRun, run_id)
        assert run is not None
        assert run.status == AgentRunStatus.FAILED.value
        assert run.error == AGENT_RUN_REAPER_INTERRUPTION_REASON
        assert run.ended_at is not None
        # Terminal-state side effect also clears the lease.
        assert run.lease_owner is None
        assert run.lease_expires_at is None

        # Audit row landed with the run's id as the lineage key.
        audit_rows = (
            (await session.execute(select(AuditLog).where(AuditLog.agent_session_id == run_id)))
            .scalars()
            .all()
        )
        assert len(audit_rows) == 1
        audit = audit_rows[0]
        assert audit.method == "INTERNAL"
        assert audit.path == "internal/agent-run/reaper/fail-into-audit"
        assert audit.status_code == 200
        assert audit.operator_sub == "system:agent-run-reaper"
        assert audit.payload["policy"] == "fail_into_audit"
        assert audit.payload["run_id"] == str(run_id)
        assert audit.payload["prior_lease_owner"] == "worker-died"


@pytest.mark.asyncio
async def test_kill_restart_run_with_resume_clears_lease_and_audits() -> None:
    """A killed run with ``resume`` policy stays ``running`` but loses its lease + audit row.

    T4 #825. The reaper does NOT transition the row to a terminal
    state -- it clears the lease so the next dispatcher sweep (T2 #823
    / T3 #824) can re-claim and a fresh worker resumes. The audit
    row records the interruption so the operator can see it happened.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        run_id = await _make_running_run_with_expired_lease(
            session,
            policy=ScheduledTriggerInFlightPolicy.RESUME,
        )

    await _run_one_tick()

    async with sessionmaker() as session:
        run = await session.get(AgentRun, run_id)
        assert run is not None
        # Status unchanged -- the run is *eligible for resume*, not
        # terminal. The dispatcher (T2/T3) re-claims and a fresh
        # worker continues from its recorded state.
        assert run.status == AgentRunStatus.RUNNING.value
        # Lease cleared -- ready for a fresh claim.
        assert run.lease_owner is None
        assert run.lease_expires_at is None

        audit_rows = (
            (await session.execute(select(AuditLog).where(AuditLog.agent_session_id == run_id)))
            .scalars()
            .all()
        )
        assert len(audit_rows) == 1
        audit = audit_rows[0]
        assert audit.path == "internal/agent-run/reaper/clear-for-resume"
        assert audit.payload["policy"] == "resume"


# ---------------------------------------------------------------------------
# Filtering / claim-query correctness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reaper_ignores_running_run_with_fresh_lease() -> None:
    """A healthy worker (lease in the future) is NOT reaped.

    T4 #825 -- the *expired* in the claim predicate is load-bearing.
    Any run with ``lease_expires_at >= now()`` is the healthy worker
    holding its lease and must not be touched.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session, slug=f"t-{uuid.uuid4().hex[:8]}")
        run = await create_run(
            session,
            tenant_id=tenant_id,
            identity_sub="user-alive",
            trigger=AgentRunTrigger.SCHEDULED,
            model_tier="deep",
        )
        await claim_lease(session, run, owner="worker-alive", ttl_seconds=300)
        await transition(session, run, AgentRunStatus.RUNNING)
        await session.commit()
        run_id = run.id

    await _run_one_tick()

    async with sessionmaker() as session:
        run = await session.get(AgentRun, run_id)
        assert run is not None
        assert run.status == AgentRunStatus.RUNNING.value
        assert run.lease_owner == "worker-alive"  # untouched
        # No audit row written.
        audit_rows = (
            (await session.execute(select(AuditLog).where(AuditLog.agent_session_id == run_id)))
            .scalars()
            .all()
        )
        assert audit_rows == []


@pytest.mark.asyncio
async def test_reaper_ignores_terminal_runs_with_stale_lease() -> None:
    """A ``failed`` / ``succeeded`` / ``cancelled`` run is not reaped.

    T4 #825 -- the ``status='running'`` claim predicate excludes
    terminal rows even if a stale lease somehow remained on them.
    Defensive: the lifecycle service clears the lease on terminal
    transitions, but the reaper's filter must still hold if a future
    code path forgets to.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session, slug=f"t-{uuid.uuid4().hex[:8]}")
        run = await create_run(
            session,
            tenant_id=tenant_id,
            identity_sub="user-finished",
            trigger=AgentRunTrigger.SCHEDULED,
            model_tier="deep",
        )
        # Force the row to ``failed`` with a stale lease still present
        # (bypassing the lifecycle service's terminal-state lease clear).
        run.status = AgentRunStatus.FAILED.value
        run.lease_owner = "worker-stale"
        run.lease_expires_at = datetime.now(UTC) - timedelta(seconds=600)
        run.ended_at = datetime.now(UTC)
        await session.commit()
        run_id = run.id

    await _run_one_tick()

    async with sessionmaker() as session:
        run = await session.get(AgentRun, run_id)
        assert run is not None
        # Status untouched -- the reaper does not "re-reap" a row.
        assert run.status == AgentRunStatus.FAILED.value
        # No audit row written.
        audit_rows = (
            (await session.execute(select(AuditLog).where(AuditLog.agent_session_id == run_id)))
            .scalars()
            .all()
        )
        assert audit_rows == []


@pytest.mark.asyncio
async def test_reaper_handles_empty_table_cleanly() -> None:
    """An empty agent_run table is the no-op fast path.

    T4 #825 -- the most common state. The reaper's claim query
    returns zero rows; the tick completes without an exception or
    a stray audit row.
    """
    # Don't seed anything beyond what's already in the schema template.
    await _run_one_tick()

    # No assertion needed -- the test passes if no exception was
    # raised. We add a sanity check that we have no orphan audit
    # rows for nonexistent runs.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        reaper_audits = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.operator_sub == "system:agent-run-reaper")
                )
            )
            .scalars()
            .all()
        )
        assert reaper_audits == []


@pytest.mark.asyncio
async def test_reaper_processes_batch_under_per_tick_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backlog of N expired runs is drained across ticks bounded by ``max_per_tick``.

    T4 #825 -- per-tick bound. Lower the limit to 2, seed 5 expired
    runs, run one tick -> exactly 2 are reaped. Run a second tick ->
    2 more. Third tick -> the final 1.
    """
    monkeypatch.setenv("AGENT_RUN_REAPER_MAX_PER_TICK", "2")
    get_settings.cache_clear()

    sessionmaker = get_sessionmaker()
    run_ids: list[uuid.UUID] = []
    async with sessionmaker() as session:
        for _ in range(5):
            run_id = await _make_running_run_with_expired_lease(
                session,
                policy=ScheduledTriggerInFlightPolicy.FAIL_INTO_AUDIT,
            )
            run_ids.append(run_id)

    async def count_failed() -> int:
        async with sessionmaker() as s:
            rows = (
                (
                    await s.execute(
                        select(AgentRun).where(
                            AgentRun.id.in_(run_ids),
                            AgentRun.status == AgentRunStatus.FAILED.value,
                        )
                    )
                )
                .scalars()
                .all()
            )
            return len(rows)

    await _run_one_tick()
    assert await count_failed() == 2

    await _run_one_tick()
    assert await count_failed() == 4

    await _run_one_tick()
    assert await count_failed() == 5

    # No further work to do -- a fourth tick is a no-op.
    await _run_one_tick()
    assert await count_failed() == 5


@pytest.mark.asyncio
async def test_reaper_mixed_policies_apply_independently() -> None:
    """One tick with both policies in the batch routes each correctly.

    T4 #825 -- per-row policy dispatch. Seed two expired runs, one
    ``fail_into_audit`` and one ``resume``. Run one tick. Assert
    each landed in its policy's outcome state and the audit rows
    have distinct paths.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        fail_id = await _make_running_run_with_expired_lease(
            session,
            policy=ScheduledTriggerInFlightPolicy.FAIL_INTO_AUDIT,
        )
        resume_id = await _make_running_run_with_expired_lease(
            session,
            policy=ScheduledTriggerInFlightPolicy.RESUME,
        )

    await _run_one_tick()

    async with sessionmaker() as session:
        fail_row = await session.get(AgentRun, fail_id)
        resume_row = await session.get(AgentRun, resume_id)
        assert fail_row is not None and resume_row is not None
        assert fail_row.status == AgentRunStatus.FAILED.value
        assert resume_row.status == AgentRunStatus.RUNNING.value
        assert resume_row.lease_owner is None

        # Two distinct audit rows, one per policy outcome.
        all_audits = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.agent_session_id.in_([fail_id, resume_id]))
                    .order_by(AuditLog.path)
                )
            )
            .scalars()
            .all()
        )
        assert len(all_audits) == 2
        paths = {a.path for a in all_audits}
        assert paths == {
            "internal/agent-run/reaper/clear-for-resume",
            "internal/agent-run/reaper/fail-into-audit",
        }
