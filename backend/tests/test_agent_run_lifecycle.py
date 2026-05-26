# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the agent-run lifecycle service.

Initiative #802 (G11.1 Agent runtime), Task #813 (T6). Covers
:mod:`meho_backplane.operations.agent_run` -- the create / inspect /
transition / cancel surface and its **enforced** state machine.

Coverage matrix
---------------

* **create_run** inserts a ``pending`` row and hands back the lineage
  key (its ``id`` == the ``agent_session_id``).
* **get_run** reads it back; returns ``None`` for an absent id.
* **Every legal transition** on :data:`ALLOWED_TRANSITIONS` succeeds and
  stamps ``started_at`` / ``ended_at`` at the right edges.
* **Every illegal transition** raises :class:`IllegalTransitionError`
  before any DB write; the persisted status is unchanged.
* **start_run / succeed_run / fail_run / increment_turns** record their
  payload (provider+model / output / error / turn count) and move status.
* **cancel_run** cancels a non-terminal run for an authorized operator;
  rejects an under-privileged operator (403-class), an already-terminal
  run (409-class), and a missing id (404-class).

Runs synchronously against the ``sqlite+aiosqlite`` engine the autouse
``_default_database_url`` fixture pre-migrates to head.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentRun,
    AgentRunStatus,
    AgentRunTrigger,
    ScheduledTriggerInFlightPolicy,
    Tenant,
)
from meho_backplane.operations.agent_run import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    AgentRunNotFoundError,
    IllegalTransitionError,
    LeaseLostError,
    UnauthorizedCancellationError,
    cancel_run,
    claim_lease,
    create_run,
    fail_run,
    get_run,
    heartbeat,
    increment_turns,
    release_lease,
    snapshot_in_flight_policy,
    start_run,
    succeed_run,
    transition,
)
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _seed_tenant(session: AsyncSession, *, slug: str = "rdc-internal") -> uuid.UUID:
    """Return the tenant row's id, inserting it on the first call (FK parent for agent_run).

    The look-up-then-insert shape is load-bearing: migration ``0018``
    seeds the ``rdc-internal`` tenant into the per-worker schema
    template (:func:`tests.conftest._schema_template_db`), so a plain
    ``session.add(Tenant(slug='rdc-internal', ...))`` would trip
    ``UNIQUE constraint failed: tenant.slug``.
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


def _operator(*, role: TenantRole, sub: str = "op-1") -> Operator:
    """Build an :class:`Operator` with the given role for cancel-auth tests."""
    return Operator(
        sub=sub,
        raw_jwt="fake.jwt.value",
        tenant_id=uuid.uuid4(),
        tenant_role=role,
    )


# ---------------------------------------------------------------------------
# create / get
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_run_inserts_pending_row_and_returns_lineage_key() -> None:
    """``create_run`` inserts a ``pending`` row; its id is the session lineage key."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        run = await create_run(
            session,
            tenant_id=tenant_id,
            identity_sub="user-7",
            identity_act="agent-triage",
            trigger=AgentRunTrigger.DIRECT,
            model_tier="cheap",
        )
        await session.commit()
        run_id = run.id

    assert isinstance(run_id, uuid.UUID)
    assert run.status == AgentRunStatus.PENDING.value
    assert run.turns == 0
    assert run.identity_sub == "user-7"
    assert run.identity_act == "agent-triage"
    assert run.trigger == AgentRunTrigger.DIRECT.value

    # The id is the agent_session_id lineage key consumed by G11.4/C2 --
    # it must resolve to the same row on a fresh read.
    async with sessionmaker() as session:
        loaded = await get_run(session, run_id)
    assert loaded is not None
    assert loaded.id == run_id


@pytest.mark.asyncio
async def test_get_run_returns_none_for_absent_id() -> None:
    """``get_run`` returns ``None`` (not an error) for a missing row."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await get_run(session, uuid.uuid4())
    assert result is None


# ---------------------------------------------------------------------------
# State machine: legal + illegal transitions
# ---------------------------------------------------------------------------


async def _make_run(
    session: AsyncSession,
    *,
    status: AgentRunStatus = AgentRunStatus.PENDING,
) -> AgentRun:
    """Create a run and force it into *status* directly (test setup helper).

    Bypasses the transition guard to position a row at an arbitrary state
    for the transition-matrix tests; production code never writes status
    except through :func:`transition`.
    """
    tenant_id = await _seed_tenant(session, slug=f"t-{uuid.uuid4().hex[:8]}")
    run = await create_run(
        session,
        tenant_id=tenant_id,
        identity_sub="user-sm",
        trigger=AgentRunTrigger.DIRECT,
        model_tier="cheap",
    )
    if status is not AgentRunStatus.PENDING:
        run.status = status.value
        await session.flush()
    return run


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    # ``ALLOWED_TRANSITIONS`` maps each state to a ``frozenset`` of
    # targets; iterating a set is nondeterministic across processes
    # (hash-seed dependent). pytest-xdist requires every worker to
    # collect identical test ids in identical order, so sort the inner
    # targets by their ``str`` value before flattening the edge list.
    [
        (frm, to)
        for frm, tos in ALLOWED_TRANSITIONS.items()
        for to in sorted(tos, key=lambda s: s.value)
    ],
)
@pytest.mark.asyncio
async def test_legal_transitions_succeed(
    from_status: AgentRunStatus,
    to_status: AgentRunStatus,
) -> None:
    """Every edge on :data:`ALLOWED_TRANSITIONS` is accepted by :func:`transition`."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        run = await _make_run(session, status=from_status)
        result = await transition(session, run, to_status)
        await session.commit()
        assert result.status == to_status.value


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [
        (frm, to)
        for frm in AgentRunStatus
        for to in AgentRunStatus
        if to not in ALLOWED_TRANSITIONS[frm] and to is not frm
    ],
)
@pytest.mark.asyncio
async def test_illegal_transitions_rejected(
    from_status: AgentRunStatus,
    to_status: AgentRunStatus,
) -> None:
    """Every edge NOT on the map raises before any DB write; status unchanged."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        run = await _make_run(session, status=from_status)
        run_id = run.id
        # Commit the positioned row so the "status unchanged" assertion
        # below reads a durable baseline -- the raising transition itself
        # is what must not persist a change.
        await session.commit()
        with pytest.raises(IllegalTransitionError) as exc:
            await transition(session, run, to_status)
        assert exc.value.from_status is from_status
        assert exc.value.to_status is to_status

    # The rejected edge left no persisted change.
    async with sessionmaker() as session:
        loaded = await get_run(session, run_id)
    assert loaded is not None
    assert loaded.status == from_status.value


@pytest.mark.asyncio
async def test_terminal_states_have_no_successors() -> None:
    """Each terminal status maps to an empty successor set (cannot move on)."""
    for status in TERMINAL_STATUSES:
        assert ALLOWED_TRANSITIONS[status] == frozenset()


# ---------------------------------------------------------------------------
# Timestamp stamping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transition_stamps_started_and_ended_at() -> None:
    """``running`` stamps ``started_at``; a terminal state stamps ``ended_at``."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        run = await _make_run(session, status=AgentRunStatus.PENDING)
        assert run.started_at is None
        assert run.ended_at is None

        await transition(session, run, AgentRunStatus.RUNNING)
        assert run.started_at is not None
        assert run.ended_at is None
        started = run.started_at

        await transition(session, run, AgentRunStatus.SUCCEEDED)
        assert run.ended_at is not None
        # started_at is not reset by the second transition.
        assert run.started_at == started
        await session.commit()


@pytest.mark.asyncio
async def test_resume_from_awaiting_approval_does_not_reset_started_at() -> None:
    """``running`` -> ``awaiting_approval`` -> ``running`` keeps the original start."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        run = await _make_run(session, status=AgentRunStatus.PENDING)
        await transition(session, run, AgentRunStatus.RUNNING)
        first_start = run.started_at
        assert first_start is not None

        await transition(session, run, AgentRunStatus.AWAITING_APPROVAL)
        await transition(session, run, AgentRunStatus.RUNNING)
        await session.commit()

        assert run.started_at == first_start


# ---------------------------------------------------------------------------
# Payload-recording helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_run_records_resolved_provider_and_model() -> None:
    """``start_run`` records provider+model and moves ``pending`` -> ``running``."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        run = await _make_run(session, status=AgentRunStatus.PENDING)
        await start_run(session, run, provider="anthropic", model="claude-opus-4")
        await session.commit()

    assert run.status == AgentRunStatus.RUNNING.value
    assert run.provider == "anthropic"
    assert run.model == "claude-opus-4"
    assert run.started_at is not None


@pytest.mark.asyncio
async def test_increment_turns_counts_up_without_status_change() -> None:
    """``increment_turns`` bumps the counter and leaves status untouched."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        run = await _make_run(session, status=AgentRunStatus.RUNNING)
        await increment_turns(session, run)
        await increment_turns(session, run)
        await session.commit()

    assert run.turns == 2
    assert run.status == AgentRunStatus.RUNNING.value


@pytest.mark.asyncio
async def test_succeed_run_records_output_and_terminates() -> None:
    """``succeed_run`` records output, moves to ``succeeded``, stamps ``ended_at``."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        run = await _make_run(session, status=AgentRunStatus.RUNNING)
        await succeed_run(session, run, output={"verdict": "ok"})
        await session.commit()

    assert run.status == AgentRunStatus.SUCCEEDED.value
    assert run.output == {"verdict": "ok"}
    assert run.ended_at is not None
    # cost stays NULL in v0.2 (stub until C3) when not supplied.
    assert run.cost is None


@pytest.mark.asyncio
async def test_succeed_run_accepts_cost_for_c3() -> None:
    """``succeed_run`` writes ``cost`` when supplied (forward-compat for C3)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        run = await _make_run(session, status=AgentRunStatus.RUNNING)
        await succeed_run(session, run, output={"verdict": "ok"}, cost=Decimal("0.0042"))
        await session.commit()

    assert run.cost == Decimal("0.0042")


@pytest.mark.asyncio
async def test_fail_run_records_error_and_terminates() -> None:
    """``fail_run`` records the error, moves to ``failed``, leaves output NULL."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        run = await _make_run(session, status=AgentRunStatus.RUNNING)
        await fail_run(session, run, error="ValueError: budget exhausted")
        await session.commit()

    assert run.status == AgentRunStatus.FAILED.value
    assert run.error == "ValueError: budget exhausted"
    assert run.output is None
    assert run.ended_at is not None


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [AgentRunStatus.PENDING, AgentRunStatus.RUNNING, AgentRunStatus.AWAITING_APPROVAL],
)
@pytest.mark.asyncio
async def test_cancel_run_cancels_non_terminal_run_for_authorized_operator(
    status: AgentRunStatus,
) -> None:
    """An OPERATOR can cancel a run in any non-terminal state."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        run = await _make_run(session, status=status)
        run_id = run.id
        operator = _operator(role=TenantRole.OPERATOR)
        cancelled = await cancel_run(session, run_id, operator=operator)
        await session.commit()

    assert cancelled.status == AgentRunStatus.CANCELLED.value
    assert cancelled.ended_at is not None


@pytest.mark.asyncio
async def test_cancel_run_allows_tenant_admin() -> None:
    """A TENANT_ADMIN (ranks above OPERATOR) can also cancel."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        run = await _make_run(session, status=AgentRunStatus.RUNNING)
        run_id = run.id
        cancelled = await cancel_run(
            session, run_id, operator=_operator(role=TenantRole.TENANT_ADMIN)
        )
        await session.commit()
    assert cancelled.status == AgentRunStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_cancel_run_rejects_read_only_operator() -> None:
    """A READ_ONLY operator may not cancel a run (403-class)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        run = await _make_run(session, status=AgentRunStatus.RUNNING)
        run_id = run.id
        # Commit the running row so the post-rejection read has a durable
        # baseline; the authorization failure must leave it untouched.
        await session.commit()
        with pytest.raises(UnauthorizedCancellationError) as exc:
            await cancel_run(session, run_id, operator=_operator(role=TenantRole.READ_ONLY))
        assert exc.value.role is TenantRole.READ_ONLY

    # The run is unchanged -- still running, not cancelled.
    async with sessionmaker() as session:
        loaded = await get_run(session, run_id)
    assert loaded is not None
    assert loaded.status == AgentRunStatus.RUNNING.value


@pytest.mark.parametrize(
    "status",
    [AgentRunStatus.SUCCEEDED, AgentRunStatus.FAILED, AgentRunStatus.CANCELLED],
)
@pytest.mark.asyncio
async def test_cancel_run_rejects_already_terminal_run(status: AgentRunStatus) -> None:
    """Cancelling a terminal run raises :class:`IllegalTransitionError` (409-class)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        run = await _make_run(session, status=status)
        run_id = run.id
        with pytest.raises(IllegalTransitionError):
            await cancel_run(session, run_id, operator=_operator(role=TenantRole.OPERATOR))


@pytest.mark.asyncio
async def test_cancel_run_raises_not_found_for_missing_id() -> None:
    """Cancelling a missing run raises :class:`AgentRunNotFoundError` (404-class)."""
    sessionmaker = get_sessionmaker()
    missing = uuid.uuid4()
    async with sessionmaker() as session:
        with pytest.raises(AgentRunNotFoundError) as exc:
            await cancel_run(session, missing, operator=_operator(role=TenantRole.OPERATOR))
        assert exc.value.run_id == missing


# ---------------------------------------------------------------------------
# Lease / heartbeat / release / snapshot (T4 #825)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_lease_stamps_owner_and_expiry() -> None:
    """:func:`claim_lease` writes ``lease_owner`` + ``lease_expires_at``.

    T4 #825 -- the worker's claim handshake. Status stays unchanged
    (the caller composes claim_lease + start_run inside one
    transaction).
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        run = await _make_run(session, status=AgentRunStatus.PENDING)
        await claim_lease(session, run, owner="worker-a", ttl_seconds=60)
        await session.commit()
        run_id = run.id

    async with sessionmaker() as session:
        loaded = await get_run(session, run_id)
        assert loaded is not None
        assert loaded.lease_owner == "worker-a"
        assert loaded.lease_expires_at is not None
        # Status untouched -- claim_lease is a pure side-effect on lease columns.
        assert loaded.status == AgentRunStatus.PENDING.value


@pytest.mark.asyncio
async def test_heartbeat_extends_lease_for_holder() -> None:
    """:func:`heartbeat` bumps ``lease_expires_at`` when the owner matches.

    T4 #825. Conditional UPDATE -- atomic at the DB layer; the new
    expiry value is what the caller sees after the helper returns.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        run = await _make_run(session, status=AgentRunStatus.PENDING)
        await claim_lease(session, run, owner="worker-a", ttl_seconds=10)
        # Walk to running so the heartbeat's status guard fires.
        await transition(session, run, AgentRunStatus.RUNNING)
        original_expiry = run.lease_expires_at
        await session.commit()
        run_id = run.id

    assert original_expiry is not None

    # New session -- production callers heartbeat from a fresh request.
    async with sessionmaker() as session:
        bumped = await heartbeat(session, run_id=run_id, owner="worker-a", ttl_seconds=60)
        await session.commit()
    assert bumped.lease_expires_at is not None
    # SQLite drops tzinfo on read-back; compare wall-clock by
    # normalising both sides to naive UTC. The production PG path
    # preserves tz, so the production semantics are still
    # "tz-aware > tz-aware".
    assert bumped.lease_expires_at.replace(tzinfo=None) > original_expiry.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_heartbeat_raises_lease_lost_when_owner_mismatches() -> None:
    """A heartbeat from the wrong owner raises :class:`LeaseLostError`.

    T4 #825 -- the worker's signal that its lease was stolen (by the
    reaper or another claimer). Conditional UPDATE touches zero rows
    -> raise.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        run = await _make_run(session, status=AgentRunStatus.PENDING)
        await claim_lease(session, run, owner="worker-a", ttl_seconds=60)
        await transition(session, run, AgentRunStatus.RUNNING)
        await session.commit()
        run_id = run.id

    async with sessionmaker() as session:
        with pytest.raises(LeaseLostError) as exc:
            await heartbeat(session, run_id=run_id, owner="impostor", ttl_seconds=60)
        assert exc.value.run_id == run_id
        assert exc.value.owner == "impostor"


@pytest.mark.asyncio
async def test_heartbeat_raises_lease_lost_when_run_already_terminal() -> None:
    """A heartbeat against a non-running row raises :class:`LeaseLostError`.

    T4 #825 -- the worker must stop on a status that's no longer
    ``running``. Covers the operator-cancel-mid-flight race: the
    cancel landed first, the worker tries to heartbeat, the UPDATE
    predicate fails the status check, the worker gets the signal to
    stop.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        run = await _make_run(session, status=AgentRunStatus.PENDING)
        await claim_lease(session, run, owner="worker-a", ttl_seconds=60)
        await transition(session, run, AgentRunStatus.RUNNING)
        # The transition() to a terminal state clears the lease as a
        # side effect -- but we want to exercise the "status guard
        # caught it" branch specifically. Use the operator-cancel
        # path which goes through the same transition guard.
        await transition(session, run, AgentRunStatus.CANCELLED)
        await session.commit()
        run_id = run.id

    async with sessionmaker() as session:
        with pytest.raises(LeaseLostError):
            await heartbeat(session, run_id=run_id, owner="worker-a", ttl_seconds=60)


@pytest.mark.asyncio
async def test_release_lease_clears_owner_and_expiry() -> None:
    """:func:`release_lease` nulls both lease columns without touching status."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        run = await _make_run(session, status=AgentRunStatus.PENDING)
        await claim_lease(session, run, owner="worker-a", ttl_seconds=60)
        await release_lease(session, run)
        await session.commit()
        run_id = run.id

    async with sessionmaker() as session:
        loaded = await get_run(session, run_id)
        assert loaded is not None
        assert loaded.lease_owner is None
        assert loaded.lease_expires_at is None
        # Status untouched.
        assert loaded.status == AgentRunStatus.PENDING.value


@pytest.mark.asyncio
async def test_terminal_transition_clears_lease_as_side_effect() -> None:
    """``transition()`` clears the lease on any terminal status.

    T4 #825 -- terminal-state side effect. A succeeded / failed /
    cancelled run must not retain stale lease metadata (the partial
    index would carry zombie entries; the reaper would skip them
    because status != 'running', but the columns themselves should
    reflect "no worker holds this").
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        run = await _make_run(session, status=AgentRunStatus.PENDING)
        await claim_lease(session, run, owner="worker-a", ttl_seconds=60)
        await transition(session, run, AgentRunStatus.RUNNING)
        await transition(session, run, AgentRunStatus.SUCCEEDED)
        await session.commit()
        run_id = run.id

    async with sessionmaker() as session:
        loaded = await get_run(session, run_id)
        assert loaded is not None
        assert loaded.status == AgentRunStatus.SUCCEEDED.value
        assert loaded.lease_owner is None
        assert loaded.lease_expires_at is None
        # ended_at still stamped -- the terminal-state side effects compose.
        assert loaded.ended_at is not None


@pytest.mark.asyncio
async def test_snapshot_in_flight_policy_overwrites_column() -> None:
    """:func:`snapshot_in_flight_policy` writes the trigger's policy onto the row.

    T4 #825 -- the run-start handshake. A definition edit mid-flight
    cannot flip behavior because the run carries its own copy.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        run = await _make_run(session, status=AgentRunStatus.PENDING)
        # Default is FAIL_INTO_AUDIT.
        assert run.in_flight_policy == ScheduledTriggerInFlightPolicy.FAIL_INTO_AUDIT.value
        await snapshot_in_flight_policy(session, run, ScheduledTriggerInFlightPolicy.RESUME)
        await session.commit()
        run_id = run.id

    async with sessionmaker() as session:
        loaded = await get_run(session, run_id)
        assert loaded is not None
        assert loaded.in_flight_policy == ScheduledTriggerInFlightPolicy.RESUME.value
