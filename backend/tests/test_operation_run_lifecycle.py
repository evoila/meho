# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the operation-run lifecycle service (#3079).

Covers :mod:`meho_backplane.operations.operation_run` -- the create /
inspect / transition / lease / cancel surface and its **enforced** state
machine, plus the closed-enum ``CHECK`` drift guards against
:class:`~meho_backplane.db.models.OperationRunStatus` /
:class:`~meho_backplane.db.models.OperationRunOrigin`.

Runs synchronously against the ``sqlite+aiosqlite`` engine the autouse
``_default_database_url`` fixture pre-migrates to head.
"""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    _OPERATION_RUN_ORIGINS,
    _OPERATION_RUN_STATUSES,
    OperationRun,
    OperationRunOrigin,
    OperationRunStatus,
    Tenant,
)
from meho_backplane.operations.operation_run import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    IllegalOperationRunTransitionError,
    OperationRunLeaseLostError,
    OperationRunNotFoundError,
    UnauthorizedOperationRunCancellationError,
    cancel_run,
    claim_lease,
    create_run,
    fail_run,
    get_run,
    heartbeat,
    list_runs,
    mark_running,
    release_lease,
    succeed_run,
    transition,
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


async def _seed_tenant(session: AsyncSession, *, slug: str = "rdc-internal") -> uuid.UUID:
    """Return the tenant row's id, inserting it on the first call (FK parent)."""
    existing: uuid.UUID | None = await session.scalar(
        select(Tenant.id).where(Tenant.slug == slug),
    )
    if existing is not None:
        return existing
    tenant_id = uuid.uuid4()
    session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
    await session.commit()
    return tenant_id


def _operator(*, tenant_id: uuid.UUID, role: TenantRole, sub: str = "op-1") -> Operator:
    return Operator(
        sub=sub,
        raw_jwt="fake.jwt.value",
        tenant_id=tenant_id,
        tenant_role=role,
    )


async def _fresh_pending(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    origin: OperationRunOrigin = OperationRunOrigin.DIRECT,
) -> OperationRun:
    return await create_run(
        session,
        tenant_id=tenant_id,
        identity_sub="op-1",
        origin=origin,
        connector_id="vault-1.x",
        op_id="secret.read",
        target_name="the-vault",
        params_hash="abc123",
    )


# ---------------------------------------------------------------------------
# create / get / list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_run_inserts_pending_row_with_coordinates() -> None:
    """``create_run`` inserts a ``pending`` row carrying the dispatch coordinates."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        run = await create_run(
            session,
            tenant_id=tenant_id,
            identity_sub="user-7",
            origin=OperationRunOrigin.DIRECT,
            connector_id="vmware-rest-9.0",
            op_id="vm.list",
            target_name="dc-vcenter",
            params_hash="deadbeef",
        )
        await session.commit()
        run_id = run.id

    assert isinstance(run_id, uuid.UUID)
    assert run.status == OperationRunStatus.PENDING.value
    assert run.origin == OperationRunOrigin.DIRECT.value
    assert run.connector_id == "vmware-rest-9.0"
    assert run.op_id == "vm.list"
    assert run.target_name == "dc-vcenter"
    assert run.params_hash == "deadbeef"
    assert run.result is None
    assert run.started_at is None and run.ended_at is None

    async with sessionmaker() as session:
        loaded = await get_run(session, run_id)
    assert loaded is not None
    assert loaded.id == run_id


@pytest.mark.asyncio
async def test_get_run_returns_none_for_absent_id() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        assert await get_run(session, uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_list_runs_tenant_isolated_status_filtered_newest_first() -> None:
    """``list_runs`` is tenant-scoped, status-filterable, newest-first, bounded."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_a = await _seed_tenant(session, slug="rdc-internal")
        tenant_b = await _seed_tenant(session, slug="other-tenant")
        older = await _fresh_pending(session, tenant_id=tenant_a)
        older.created_at = datetime.now(UTC) - timedelta(hours=1)
        newer = await _fresh_pending(session, tenant_id=tenant_a)
        await mark_running(session, newer)
        # A cross-tenant row must never surface in tenant_a's listing.
        await _fresh_pending(session, tenant_id=tenant_b)
        await session.commit()
        a_id_older, a_id_newer = older.id, newer.id

    async with sessionmaker() as session:
        rows = await list_runs(session, tenant_id=tenant_a)
        assert [r.id for r in rows] == [a_id_newer, a_id_older]  # newest first

        running = await list_runs(session, tenant_id=tenant_a, status=OperationRunStatus.RUNNING)
        assert [r.id for r in running] == [a_id_newer]

        page = await list_runs(session, tenant_id=tenant_a, limit=1)
        assert [r.id for r in page] == [a_id_newer]


# ---------------------------------------------------------------------------
# transitions + timestamps + lease clearing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_running_stamps_started_at() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        run = await _fresh_pending(session, tenant_id=tenant_id)
        await mark_running(session, run)
        await session.commit()
    assert run.status == OperationRunStatus.RUNNING.value
    assert run.started_at is not None


@pytest.mark.asyncio
async def test_terminal_transition_stamps_ended_at_and_clears_lease() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        run = await _fresh_pending(session, tenant_id=tenant_id)
        await claim_lease(session, run, owner="host:1", ttl_seconds=60)
        await mark_running(session, run)
        assert run.lease_owner == "host:1"
        await succeed_run(session, run, result={"status": "ok", "op_id": "secret.read"})
        await session.commit()
    assert run.status == OperationRunStatus.SUCCEEDED.value
    assert run.ended_at is not None
    assert run.lease_owner is None and run.lease_expires_at is None
    assert run.result == {"status": "ok", "op_id": "secret.read"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [
        (OperationRunStatus.PENDING, OperationRunStatus.SUCCEEDED),
        (OperationRunStatus.PENDING, OperationRunStatus.FAILED),
        (OperationRunStatus.SUCCEEDED, OperationRunStatus.RUNNING),
        (OperationRunStatus.FAILED, OperationRunStatus.SUCCEEDED),
        (OperationRunStatus.CANCELLED, OperationRunStatus.RUNNING),
    ],
)
async def test_illegal_transition_raises_before_write(
    from_status: OperationRunStatus, to_status: OperationRunStatus
) -> None:
    """An edge not on :data:`ALLOWED_TRANSITIONS` raises and leaves status unchanged."""
    assert to_status not in ALLOWED_TRANSITIONS[from_status]
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        run = await _fresh_pending(session, tenant_id=tenant_id)
        # Drive to the source state via legal edges without extra payload.
        run.status = from_status.value
        await session.flush()
        with pytest.raises(IllegalOperationRunTransitionError):
            await transition(session, run, to_status)
        assert run.status == from_status.value


@pytest.mark.asyncio
async def test_fail_run_records_error_distinct_from_result() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        run = await _fresh_pending(session, tenant_id=tenant_id)
        await mark_running(session, run)
        await fail_run(session, run, error="worker died")
        await session.commit()
    assert run.status == OperationRunStatus.FAILED.value
    assert run.error == "worker died"
    assert run.result is None


# ---------------------------------------------------------------------------
# lease / heartbeat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_extends_when_owner_matches_and_running() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        run = await _fresh_pending(session, tenant_id=tenant_id)
        await claim_lease(session, run, owner="host:1", ttl_seconds=1)
        await mark_running(session, run)
        first_expiry = run.lease_expires_at
        await session.commit()
        run_id = run.id
    assert first_expiry is not None

    async with sessionmaker() as session:
        refreshed = await heartbeat(session, run_id=run_id, owner="host:1", ttl_seconds=120)
        await session.commit()
    assert refreshed.lease_expires_at is not None
    # SQLite drops tzinfo on read-back; normalise both sides to naive UTC to
    # compare wall-clock. The production PG path preserves tz.
    assert refreshed.lease_expires_at.replace(tzinfo=None) > first_expiry.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_heartbeat_raises_lease_lost_on_owner_mismatch() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        run = await _fresh_pending(session, tenant_id=tenant_id)
        await claim_lease(session, run, owner="host:1", ttl_seconds=60)
        await mark_running(session, run)
        await session.commit()
        run_id = run.id

    async with sessionmaker() as session:
        with pytest.raises(OperationRunLeaseLostError):
            await heartbeat(session, run_id=run_id, owner="host:99", ttl_seconds=60)


@pytest.mark.asyncio
async def test_heartbeat_raises_lease_lost_when_terminal() -> None:
    """A cancelled (terminal) run is no longer heartbeatable (status guard)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        run = await _fresh_pending(session, tenant_id=tenant_id)
        await claim_lease(session, run, owner="host:1", ttl_seconds=60)
        await mark_running(session, run)
        await transition(session, run, OperationRunStatus.CANCELLED)
        await session.commit()
        run_id = run.id

    async with sessionmaker() as session:
        with pytest.raises(OperationRunLeaseLostError):
            await heartbeat(session, run_id=run_id, owner="host:1", ttl_seconds=60)


@pytest.mark.asyncio
async def test_release_lease_is_idempotent() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        run = await _fresh_pending(session, tenant_id=tenant_id)
        await claim_lease(session, run, owner="host:1", ttl_seconds=60)
        await release_lease(session, run)
        await release_lease(session, run)  # no-op second time
        await session.commit()
    assert run.lease_owner is None and run.lease_expires_at is None


# ---------------------------------------------------------------------------
# cancel authorization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_run_cancels_for_operator() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        run = await _fresh_pending(session, tenant_id=tenant_id)
        await session.commit()
        run_id = run.id
        operator = _operator(tenant_id=tenant_id, role=TenantRole.OPERATOR)

    async with sessionmaker() as session:
        cancelled = await cancel_run(session, run_id, operator=operator)
        await session.commit()
    assert cancelled.status == OperationRunStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_cancel_run_rejects_read_only_operator() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        run = await _fresh_pending(session, tenant_id=tenant_id)
        await session.commit()
        run_id = run.id
        operator = _operator(tenant_id=tenant_id, role=TenantRole.READ_ONLY)

    async with sessionmaker() as session:
        with pytest.raises(UnauthorizedOperationRunCancellationError):
            await cancel_run(session, run_id, operator=operator)


@pytest.mark.asyncio
async def test_cancel_run_missing_id_raises_not_found() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        operator = _operator(tenant_id=tenant_id, role=TenantRole.OPERATOR)
        with pytest.raises(OperationRunNotFoundError):
            await cancel_run(session, uuid.uuid4(), operator=operator)


@pytest.mark.asyncio
async def test_cancel_run_already_terminal_raises_illegal_transition() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        run = await _fresh_pending(session, tenant_id=tenant_id)
        await mark_running(session, run)
        await succeed_run(session, run, result={"status": "ok", "op_id": "secret.read"})
        await session.commit()
        run_id = run.id
        operator = _operator(tenant_id=tenant_id, role=TenantRole.OPERATOR)

    async with sessionmaker() as session:
        with pytest.raises(IllegalOperationRunTransitionError):
            await cancel_run(session, run_id, operator=operator)


# ---------------------------------------------------------------------------
# closed-enum CHECK constraints + drift guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_check_rejects_unknown() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        session.add(
            OperationRun(
                tenant_id=tenant_id,
                identity_sub="op-bad",
                origin=OperationRunOrigin.DIRECT.value,
                connector_id="vault-1.x",
                op_id="secret.read",
                status="not-a-real-status",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_origin_check_rejects_unknown() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        session.add(
            OperationRun(
                tenant_id=tenant_id,
                identity_sub="op-bad",
                origin="not-a-real-origin",
                connector_id="vault-1.x",
                op_id="secret.read",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


def test_terminal_statuses_are_the_three_terminal_states() -> None:
    assert {
        OperationRunStatus.SUCCEEDED,
        OperationRunStatus.FAILED,
        OperationRunStatus.CANCELLED,
    } == TERMINAL_STATUSES


def test_status_literals_match_model_enum() -> None:
    """``_OPERATION_RUN_STATUSES`` mirrors :class:`OperationRunStatus`."""
    assert set(_OPERATION_RUN_STATUSES) == {s.value for s in OperationRunStatus}


def test_origin_literals_match_model_enum() -> None:
    """``_OPERATION_RUN_ORIGINS`` mirrors :class:`OperationRunOrigin`."""
    assert set(_OPERATION_RUN_ORIGINS) == {o.value for o in OperationRunOrigin}


def _load_migration_0080() -> object:
    path = (
        Path(__file__).resolve().parent.parent
        / "alembic"
        / "versions"
        / "0080_create_operation_run.py"
    )
    spec = importlib.util.spec_from_file_location("_migration_0080", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_literals_match_model_enum() -> None:
    """Migration ``0080``'s frozen tuples match the model enums (drift guard)."""
    module = _load_migration_0080()
    assert set(module._OPERATION_RUN_STATUSES) == {s.value for s in OperationRunStatus}  # type: ignore[attr-defined]
    assert set(module._OPERATION_RUN_ORIGINS) == {o.value for o in OperationRunOrigin}  # type: ignore[attr-defined]
