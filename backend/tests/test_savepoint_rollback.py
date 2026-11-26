# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A rollback of the outer transaction also undoes the writes of its savepoints.

Each test runs one write that MEHO makes in a savepoint
(``session.begin_nested()``) on the normal engine. The savepoint write
succeeds and the same transaction sees it. Then a later write of that
transaction fails, and the transaction rolls back. A new session must find
nothing of the savepoint write. On SQLite this holds because the engine
starts a real transaction before the first savepoint
(:func:`meho_backplane.db.engine.create_engine_for_url`).

The settings import has its own file: ``test_settings_import_rollback.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import meho_backplane.agent.reaper as agent_reaper
import meho_backplane.operations.operation_run_reaper as operation_reaper
from meho_backplane.api.v1.broadcast_overrides import BroadcastOverrideCreate
from meho_backplane.db.engine import get_engine, get_sessionmaker
from meho_backplane.db.models import (
    AgentRun,
    AgentRunStatus,
    AgentRunTrigger,
    ApprovalRequest,
    ApprovalRequestKey,
    BroadcastOverride,
    OperationRun,
    OperationRunOrigin,
    OperationRunStatus,
    RunApproval,
    ScheduledTriggerInFlightPolicy,
    Tenant,
)
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.agent_run import claim_lease, create_run, transition
from meho_backplane.operations.approval_queue import create_pending_request
from meho_backplane.operations.run_approvals import _insert_one_open
from meho_backplane.settings import get_settings
from meho_backplane.ui.routes.broadcast.overrides import _try_create_in_savepoint
from meho_backplane.web_push.endpoints import CheckedSubscription
from meho_backplane.web_push.models import PushSubscription
from meho_backplane.web_push.recipients import LoginFacts
from meho_backplane.web_push.store import (
    delete_own_by_endpoint,
    refresh_login_facts,
    save_subscription,
)
from tests.test_destructive_preview_binding import _TENANT, _operator

_ENDPOINT = "https://push.example.test/send/rollback-check"
_SUB = "op-requester"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _fail_a_later_write(session: AsyncSession) -> None:
    """A write of the same transaction that the database refuses."""
    await session.execute(text("INSERT INTO no_such_table (x) VALUES (1)"))


async def _count(model: Any, *where: Any) -> int:
    async with get_sessionmaker()() as session:
        stmt = select(func.count()).select_from(model).where(*where)
        return int((await session.execute(stmt)).scalar_one())


async def _seed_tenant() -> None:
    async with get_sessionmaker()() as session, session.begin():
        session.add(Tenant(id=_TENANT, slug=f"t-{uuid.uuid4().hex[:8]}", name="Tenant"))


# ---------------------------------------------------------------------------
# Approval queue: the retry key and the reuse key
# ---------------------------------------------------------------------------


async def _park(session: AsyncSession, *, key: str | None) -> ApprovalRequest:
    params = {"name": "app-01"}
    return await create_pending_request(
        session,
        operator=_operator(sub=_SUB),
        connector_id="example-rest-1",
        op_id="POST:/vms",
        target=None,
        params=params,
        params_hash=compute_params_hash(params),
        idempotency_key=key,
        proposed_effect={"safety_level": "caution"},
    )


@pytest.mark.asyncio
async def test_waiting_request_with_retry_key_is_not_kept() -> None:
    with pytest.raises(OperationalError):
        async with get_sessionmaker()() as session, session.begin():
            request = await _park(session, key="retry-1")
            assert await session.get(ApprovalRequest, request.id) is not None
            await _fail_a_later_write(session)

    assert await _count(ApprovalRequest) == 0


@pytest.mark.asyncio
async def test_reuse_key_is_not_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPROVAL_SAME_CALL_REUSE", "true")
    get_settings.cache_clear()
    async with get_sessionmaker()() as session, session.begin():
        first = await _park(session, key=None)

    with pytest.raises(OperationalError):
        async with get_sessionmaker()() as session, session.begin():
            reused = await _park(session, key="reuse-1")
            assert reused.id == first.id
            linked = await session.scalar(select(func.count()).select_from(ApprovalRequestKey))
            assert linked == 1
            await _fail_a_later_write(session)

    assert await _count(ApprovalRequestKey) == 0
    assert await _count(ApprovalRequest) == 1


# ---------------------------------------------------------------------------
# Run approval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_run_approval_is_not_kept() -> None:
    now = datetime.now(UTC)
    row = RunApproval(
        tenant_id=_TENANT,
        principal_sub=_SUB,
        work_ref="run-rollback-1",
        entries=[{"connector_id": "example-rest-1", "op_id": "POST:/vms"}],
        reason="rollback check",
        requested_at=now,
        expires_at=now + timedelta(hours=1),
    )
    with pytest.raises(OperationalError):
        async with get_sessionmaker()() as session, session.begin():
            await _insert_one_open(session, row, now)
            assert await session.get(RunApproval, row.id) is not None
            await _fail_a_later_write(session)

    assert await _count(RunApproval) == 0


# ---------------------------------------------------------------------------
# The two reapers: the tick's own commit fails after the per-row savepoints
# ---------------------------------------------------------------------------


def _commit_fails(
    monkeypatch: pytest.MonkeyPatch, module: Any, seen: list[str], model: Any
) -> None:
    """Make the tick's commit fail; first record the status the tick wrote."""

    class _FailingCommit(AsyncSession):
        async def commit(self) -> None:
            seen.extend((await self.execute(select(model.status))).scalars())
            await _fail_a_later_write(self)

    def _sessionmaker() -> async_sessionmaker[AsyncSession]:
        return async_sessionmaker(get_engine(), class_=_FailingCommit, expire_on_commit=False)

    monkeypatch.setattr(module, "get_sessionmaker", _sessionmaker)


@pytest.mark.asyncio
async def test_reaped_agent_run_is_not_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    await _seed_tenant()
    async with get_sessionmaker()() as session:
        run = await create_run(
            session,
            tenant_id=_TENANT,
            identity_sub=_SUB,
            trigger=AgentRunTrigger.SCHEDULED,
            model_tier="deep",
        )
        run.in_flight_policy = ScheduledTriggerInFlightPolicy.FAIL_INTO_AUDIT.value
        await claim_lease(session, run, owner="worker-gone", ttl_seconds=60)
        await transition(session, run, AgentRunStatus.RUNNING)
        run.lease_expires_at = datetime.now(UTC) - timedelta(seconds=120)
        await session.commit()
        run_id = run.id

    seen: list[str] = []
    _commit_fails(monkeypatch, agent_reaper, seen, AgentRun)
    with pytest.raises(OperationalError):
        await agent_reaper._run_one_tick()

    assert seen == [AgentRunStatus.FAILED.value]
    async with get_sessionmaker()() as session:
        stored = await session.get(AgentRun, run_id)
    assert stored is not None
    assert stored.status == AgentRunStatus.RUNNING.value


@pytest.mark.asyncio
async def test_reaped_operation_run_is_not_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    await _seed_tenant()
    async with get_sessionmaker()() as session, session.begin():
        run = OperationRun(
            id=uuid.uuid4(),
            tenant_id=_TENANT,
            identity_sub=_SUB,
            origin=OperationRunOrigin.DIRECT.value,
            connector_id="example-rest-1",
            op_id="vm.create",
            status=OperationRunStatus.RUNNING.value,
            lease_owner="worker-gone",
            started_at=datetime.now(UTC) - timedelta(minutes=5),
            lease_expires_at=datetime.now(UTC) - timedelta(seconds=120),
        )
        session.add(run)

    seen: list[str] = []
    _commit_fails(monkeypatch, operation_reaper, seen, OperationRun)
    with pytest.raises(OperationalError):
        await operation_reaper._run_one_tick()

    assert seen and seen != [OperationRunStatus.RUNNING.value]
    async with get_sessionmaker()() as session:
        stored = await session.get(OperationRun, run.id)
    assert stored is not None
    assert stored.status == OperationRunStatus.RUNNING.value


# ---------------------------------------------------------------------------
# Broadcast override
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_broadcast_override_is_not_kept() -> None:
    payload = BroadcastOverrideCreate(op_id_pattern="vm.*", detail="aggregate")
    with pytest.raises(OperationalError):
        async with get_sessionmaker()() as session, session.begin():
            refused = await _try_create_in_savepoint(
                payload=payload, operator=_operator(sub=_SUB), db_session=session
            )
            assert refused is None
            assert await session.scalar(select(func.count()).select_from(BroadcastOverride)) == 1
            await _fail_a_later_write(session)

    assert await _count(BroadcastOverride) == 0


# ---------------------------------------------------------------------------
# Push subscriptions: save, delete, refresh of the login facts
# ---------------------------------------------------------------------------

_FACTS = LoginFacts(tenant_role="operator")
_SUBSCRIPTION = CheckedSubscription(endpoint=_ENDPOINT, p256dh="p256dh-key", auth="auth-key")


async def _seed_subscription() -> None:
    async with get_sessionmaker()() as session, session.begin():
        await save_subscription(
            session,
            operator_sub=_SUB,
            tenant_id=_TENANT,
            subscription=_SUBSCRIPTION,
            facts=_FACTS,
        )


async def _approver_flags() -> list[bool]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(PushSubscription.approver))).scalars())


@pytest.mark.asyncio
async def test_saved_push_subscription_is_not_kept() -> None:
    with pytest.raises(OperationalError):
        async with get_sessionmaker()() as session, session.begin():
            await save_subscription(
                session,
                operator_sub=_SUB,
                tenant_id=_TENANT,
                subscription=_SUBSCRIPTION,
                facts=_FACTS,
            )
            assert await session.scalar(select(func.count()).select_from(PushSubscription)) == 1
            await _fail_a_later_write(session)

    assert await _count(PushSubscription) == 0


@pytest.mark.asyncio
async def test_deleted_push_subscription_is_kept() -> None:
    await _seed_subscription()
    with pytest.raises(OperationalError):
        async with get_sessionmaker()() as session, session.begin():
            deleted = await delete_own_by_endpoint(session, operator_sub=_SUB, endpoint=_ENDPOINT)
            assert deleted is True
            await _fail_a_later_write(session)

    assert await _count(PushSubscription, PushSubscription.endpoint == _ENDPOINT) == 1


@pytest.mark.asyncio
async def test_refreshed_login_facts_are_not_kept() -> None:
    await _seed_subscription()
    with pytest.raises(OperationalError):
        async with get_sessionmaker()() as session, session.begin():
            await refresh_login_facts(
                session,
                operator_sub=_SUB,
                tenant_id=_TENANT,
                facts=LoginFacts(tenant_role="operator", approver=True),
            )
            flags = (await session.execute(select(PushSubscription.approver))).scalars()
            assert list(flags) == [True]
            await _fail_a_later_write(session)

    assert await _approver_flags() == [False]
